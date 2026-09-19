"""D3: request contract + poller. The plan's list: valid add; invalid symbol; unknown type; duplicate id; interrupted inflight; poll while the
lock is held -> 75. Plus history format, ordering, run-once, shell-text safety, git behaviour."""
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from conftest import child_env
from dataops import lock as lk
from dataops import poll as pl
from dataops import publish as pub
from dataops import requests_spec as rs
from gitrepo import NOW, clone, push_file, sh
from test_d2_repair_run_cli import setup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def mkreq(t, args=None, uid=None, ts="20260310T141107Z", **over):
    uid = uid or str(uuid.uuid4())
    req = {"id": uid, "type": t, "createdAt": "2026-03-10T14:11:07Z", "createdBy": "panel", "args": args if args is not None else {}}
    req.update(over)
    return "%s-%s-%s.json" % (ts, t, uid[:8]), req


def put(ctx, name, req, folder="requests"):
    d = os.path.join(ctx.repo, "data", "control", folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(req if isinstance(req, str) else json.dumps(req))


def hist(ctx, name):
    return json.load(open(os.path.join(ctx.repo, "data", "control", "history", name), encoding="utf-8"))


def ctl(ctx, *p):
    return os.path.join(ctx.repo, "data", "control", *p)


def registry(ctx):
    return json.load(open(ctx.registry_path, encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAOPS_NO_REACQUIRE", "1")
    c, w, layout = setup(tmp_path)
    open(os.path.join(c.eod2_root, "daily", "zzz.csv"), "w").write(open(os.path.join(c.eod2_root, "daily", "aaa.csv")).read())
    runs = []
    return c, layout, runs, (lambda ctx, layout, verbose=None, **k: runs.append(os.environ.get(lk.TOKEN_ENV)) or 0)


def go(c, layout, run_fn, **kw):
    lines = []
    rc = pl.poll(c, layout, now=NOW, run_fn=run_fn, verbose=lines.append, **kw)
    return rc, lines


def test_valid_add_is_executed_recorded_committed_pushed_and_triggers_one_run(env, tmp_path):
    c, layout, runs, run_fn = env
    other = clone(tmp_path, os.path.join(str(tmp_path), "repo_remote.git"), "panelclone")
    name, req = mkreq("add", {"symbol": "ZZZ", "engines": ["LIFO", "DM"]})
    push_file(other, "data/control/requests/" + name, json.dumps(req), "panel request")
    rc, lines = go(c, layout, run_fn)
    assert rc == 0 and len(runs) == 1 and runs[0]                      # registry changed -> `run` once, under the poller's (re-entrant) lock token
    h = hist(c, name)
    assert h["result"] == "OK" and "added ZZZ" in h["message"] and h["finishedAt"] == "2026-03-10T19:41:07+05:30" and h["id"] == req["id"] and h["args"] == req["args"]
    assert next(i for i in registry(c)["instruments"] if i["symbol"] == "ZZZ")["engines"] == ["LIFO", "DM"]
    assert not os.path.exists(ctl(c, "requests", name)) and os.listdir(ctl(c, "inflight")) == []
    assert json.load(open(ctl(c, "processed_ids.json"))) == [req["id"]]
    remote = sh("git", "-C", os.path.join(str(tmp_path), "repo_remote.git"), "rev-parse", "main")
    assert remote == sh("git", "-C", c.repo, "rev-parse", "HEAD")
    assert sh("git", "-C", c.repo, "log", "-1", "--format=%s") == "control: 1 request(s) processed"
    tracked = sh("git", "-C", c.repo, "ls-files", "data/control", "config").splitlines()
    assert "data/control/history/" + name in tracked and "config/universe.json" in tracked
    assert sh("git", "-C", c.repo, "status", "--short") == ""


def test_invalid_symbol_is_an_error_and_nothing_runs(env):
    c, layout, runs, run_fn = env
    name, req = mkreq("add", {"symbol": "nifty; del *"})
    put(c, name, req)
    before = open(c.registry_path, "rb").read()
    rc, _ = go(c, layout, run_fn)
    h = hist(c, name)
    assert rc == 0 and h["result"] == "ERROR" and "invalid request" in h["message"] and "symbol" in h["message"]
    assert open(c.registry_path, "rb").read() == before and runs == [] and json.load(open(ctl(c, "processed_ids.json"))) == [req["id"]]


def test_unknown_type_and_unknown_arg_and_filename_mismatch_are_errors(env):
    c, layout, runs, run_fn = env
    n1, r1 = mkreq("shutdown")
    n2, r2 = mkreq("run", {"cmd": "whoami"})
    n3, r3 = mkreq("remove", {"symbol": "AAA"})
    put(c, n1, r1)
    put(c, n2, r2)
    put(c, "20260310T141107Z-add-%s.json" % r3["id"][:8], r3)            # file name says add, content says remove
    go(c, layout, run_fn)
    assert "unknown type" in hist(c, n1)["message"] and "unknown arg" in hist(c, n2)["message"]
    assert "does not match" in hist(c, "20260310T141107Z-add-%s.json" % r3["id"][:8])["message"]
    assert all(hist(c, n)["result"] == "ERROR" for n in (n1, n2)) and runs == []
    assert next(i for i in registry(c)["instruments"] if i["symbol"] == "AAA")["enabled"] is True     # the mismatched remove did NOT run


def test_duplicate_id_is_recorded_as_duplicate_and_not_run_again(env):
    c, layout, runs, run_fn = env
    name, req = mkreq("set_engines", {"symbol": "AAA", "engines": ["TENET"]})
    put(c, name, req)
    go(c, layout, run_fn)
    assert hist(c, name)["result"] == "OK"
    name2 = "20260310T150000Z-set_engines-%s.json" % req["id"][:8]
    put(c, name2, dict(req, args={"symbol": "AAA", "engines": []}))       # same id, different content, later file
    rc, _ = go(c, layout, run_fn)
    h2 = hist(c, name2)
    assert rc == 0 and h2["result"] == "DUPLICATE" and "already processed" in h2["message"]
    assert next(i for i in registry(c)["instruments"] if i["symbol"] == "AAA")["engines"] == ["TENET"]          # the duplicate did not change anything
    assert json.load(open(ctl(c, "processed_ids.json"))) == [req["id"]]


def test_interrupted_inflight_is_moved_to_history_and_never_rerun(env):
    c, layout, runs, run_fn = env
    name, req = mkreq("remove", {"symbol": "AAA"})
    put(c, name, req, "inflight")                                         # the poller died while running this
    rc, lines = go(c, layout, run_fn)
    h = hist(c, name)
    assert rc == 0 and h["result"] == "ERROR" and "interrupted" in h["message"] and h["id"] == req["id"]
    assert next(i for i in registry(c)["instruments"] if i["symbol"] == "AAA")["enabled"] is True and runs == []
    assert os.listdir(ctl(c, "inflight")) == [] and any("interrupted" in x for x in lines)
    put(c, "garbage.json", "{not json", "inflight")
    go(c, layout, run_fn)
    assert hist(c, "garbage.json")["result"] == "ERROR"


def test_poll_is_busy_75_while_another_process_holds_the_lock(env, isolated_lock):
    c, layout, runs, run_fn = env
    name, req = mkreq("remove", {"symbol": "AAA"})
    put(c, name, req)
    holder = subprocess.Popen([sys.executable, "-m", "dataops", "lock-run", "--", sys.executable, "-c", "import time; time.sleep(10)"], cwd=SRC, env=child_env(isolated_lock))
    try:
        for _ in range(100):
            if os.path.exists(isolated_lock):
                break
            time.sleep(0.1)
        rc, lines = go(c, layout, run_fn)
    finally:
        holder.kill()
        holder.wait()
    assert rc == 75 and os.path.exists(ctl(c, "requests", name)) and not os.path.exists(ctl(c, "history", name)) and runs == []


def test_requests_run_in_file_name_order_and_a_run_request_suppresses_the_extra_run(env):
    c, layout, runs, run_fn = env
    n1, r1 = mkreq("run", ts="20260310T100000Z")
    n2, r2 = mkreq("set_engines", {"symbol": "AAA", "engines": ["LIFO"]}, ts="20260310T090000Z")
    n3, r3 = mkreq("remove", {"symbol": "BBB"}, ts="20260310T110000Z")
    for n, r in ((n1, r1), (n2, r2), (n3, r3)):
        put(c, n, r)
    rc, lines = go(c, layout, run_fn)
    order = [l.split()[1] for l in lines if l.startswith("poll: 2026")]
    assert order == [n2, n1, n3] and len(runs) == 1                        # explicit `run` request already ran; no extra end-of-batch run
    assert [hist(c, n)["result"] for n in (n1, n2, n3)] == ["OK", "OK", "OK"]


def test_read_only_requests_do_not_trigger_a_run_and_diagnose_returns_the_report(env):
    c, layout, runs, run_fn = env
    n1, r1 = mkreq("diagnose", {"symbol": "AAA", "from": "2025-06-02", "to": "2025-06-06"})
    put(c, n1, r1)
    rc, _ = go(c, layout, run_fn)
    h = hist(c, n1)
    assert rc == 0 and h["result"] == "OK" and h["message"].startswith("DIAGNOSIS AAA") and runs == []


def test_executor_errors_become_error_results(env):
    c, layout, runs, run_fn = env
    n1, r1 = mkreq("add", {"symbol": "NOSUCHETF"})                         # not in EOD2
    n2, r2 = mkreq("approve_move", {"symbol": "NOPE", "date": "2025-09-01", "evidence": "x"})
    n3, r3 = mkreq("backfill", {"symbol": "AAA", "from": "2025-06-02"})
    for n, r in ((n1, r1), (n2, r2), (n3, r3)):
        put(c, n, r)
    go(c, layout, run_fn)
    assert hist(c, n1)["result"] == "ERROR" and "NOT_IN_EOD2" in hist(c, n1)["message"]
    assert hist(c, n2)["result"] == "ERROR" and "not in the registry" in hist(c, n2)["message"]
    assert hist(c, n3)["result"] == "OK"
    assert len(runs) == 1                                                  # only the successful registry change triggered the run


def test_invalid_json_request_file_is_an_error_record(env):
    c, layout, runs, run_fn = env
    put(c, "20260310T141107Z-run-abcd1234.json", "{oops")
    go(c, layout, run_fn)
    h = hist(c, "20260310T141107Z-run-abcd1234.json")
    assert h["result"] == "ERROR" and "invalid JSON" in h["message"] and runs == []


def test_shell_text_in_a_request_is_never_executed(env, tmp_path):
    c, layout, runs, run_fn = env
    marker = tmp_path / "pwned.txt"
    for sym, ev in (("AAA; echo hi > %s" % marker, "x"), ("AAA", "$(echo hi > %s)" % marker), ("AAA", "`echo hi > %s`" % marker)):
        n, r = mkreq("set_listing", {"symbol": sym, "date": "2020-01-01", "evidence": ev})
        put(c, n, r)
    go(c, layout, run_fn)
    assert not marker.exists()
    # the evidence variants are harmless text: recorded verbatim, never interpreted
    ev = registry(c)["instruments"][0]["listingDateEvidence"]
    assert ev in ("`echo hi > %s`" % marker, "$(echo hi > %s)" % marker)


def test_processed_ids_keeps_the_last_500(env):
    c, layout, runs, run_fn = env
    ids = ["%08d-0000-4000-8000-000000000000" % i for i in range(495)]
    with open(ctl(c, "processed_ids.json") if os.makedirs(ctl(c), exist_ok=True) is None else "", "w") as f:
        json.dump(ids, f)
    for i in range(10):
        n, r = mkreq("rebuild_missing" if False else "diagnose", {"symbol": "AAA"}, ts="20260310T1%05dZ" % i)
        put(c, n, r)
    go(c, layout, run_fn)
    kept = json.load(open(ctl(c, "processed_ids.json")))
    assert len(kept) == 500 and kept[0] == ids[5] and kept[-1] not in ids


def test_pull_failure_is_exit_1_and_nothing_is_touched(env, tmp_path):
    c, layout, runs, run_fn = env
    n, r = mkreq("remove", {"symbol": "AAA"})
    put(c, n, r)
    sh("git", "-C", c.repo, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    rc, lines = go(c, layout, run_fn)
    assert rc == 1 and any("git pull --ff-only failed" in x for x in lines) and os.path.exists(ctl(c, "requests", n))


def test_push_conflict_with_a_newer_request_rebases_and_succeeds(env, tmp_path):
    c, layout, runs, run_fn = env
    bare = os.path.join(str(tmp_path), "repo_remote.git")
    other = clone(tmp_path, bare, "other")
    n, r = mkreq("set_engines", {"symbol": "AAA", "engines": ["DM"]})
    put(c, n, r)
    real = pub.git
    state = {"n": 0}

    def racing(repo, *args, check=True):
        if args[:1] == ("commit",) and state["n"] == 0:
            state["n"] = 1
            n2, r2 = mkreq("run", ts="20260310T160000Z")
            push_file(other, "data/control/requests/" + n2, json.dumps(r2), "a newer panel request")
        return real(repo, *args, check=check)
    pub.git = racing
    try:
        rc, _ = go(c, layout, run_fn)
    finally:
        pub.git = real
    assert rc == 0 and sh("git", "-C", bare, "rev-parse", "main") == sh("git", "-C", c.repo, "rev-parse", "HEAD")
    assert any(f.endswith("-run-%s.json" % f.split("-")[-1][:-5]) or "-run-" in f for f in os.listdir(ctl(c, "requests")))      # the newer request survived for the next poll


def test_the_request_spec_module_matches_the_shared_cases():
    cases = json.load(open(os.path.join(ROOT, "tests", "data", "request_cases.json"), encoding="utf-8"))["cases"]
    assert len(cases) >= 60
    for c in cases:
        errs = rs.validate_request(c["req"], c.get("file"))
        assert (not errs) == c["valid"], (c["name"], errs)
        if c.get("errorContains"):
            assert c["errorContains"] in " | ".join(errs), (c["name"], errs)
    assert {c["req"]["type"] for c in cases if c["valid"] and isinstance(c["req"], dict)} == set(rs.TYPES)


def test_cli_poll_end_to_end(env, tmp_path, isolated_lock):
    c, layout, runs, run_fn = env
    n, r = mkreq("diagnose", {"symbol": "AAA", "from": "2025-06-02", "to": "2025-06-06"})
    put(c, n, r)
    lay = str(tmp_path / "layout.json")
    json.dump(layout, open(lay, "w"))
    r_ = subprocess.run([sys.executable, "-m", "dataops", "poll"], cwd=SRC, capture_output=True, text=True,
                        env=child_env(isolated_lock, DATAOPS_REPO=c.repo, DATAOPS_EOD2_ROOT=c.eod2_root, DATAOPS_V2_OUT=c.v2_out, DATAOPS_V2_BACKUP=c.v2_backup, DATAOPS_LAYOUT=lay, DATAOPS_NO_REACQUIRE="1"))
    assert r_.returncode == 0, r_.stderr
    assert json.load(open(ctl(c, "history", n)))["result"] == "OK"
