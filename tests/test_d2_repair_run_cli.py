"""D2: repair, the run pipeline (sync -> export -> publish -> notify), rebuild-missing and the CLI in a child process."""
import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from conftest import child_env
from dataops import export as ex
from dataops import lock as lk
from dataops import ops
from dataops import publish as pub
from dataops import repair as rp
from dataops import run as runner
from dataops import sync as sy
from eod2_factory import World, make_eod2, make_registry, walk, write_registry
from gitrepo import NOW, init_pair, sh, small_registry_file

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def tree_sig(root):
    return {os.path.join(d, f): hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest() for d, _, fs in os.walk(root) for f in fs}


def setup(tmp_path, **root_kw):
    """repo (with bare remote) + synthetic EOD2 + registry; returns (Ctx, world, layout)."""
    w = World(tmp_path)
    root = w.build(**root_kw)
    repo, bare = init_pair(tmp_path)
    small_registry_file(repo)
    c = ops.Ctx(repo=repo, eod2_root=root, v2_out=str(tmp_path / "v2.json"), v2_backup=str(tmp_path / "v2.bak.json"), now=NOW)
    layout = {"eod2Src": str(tmp_path / "src"), "dataRoot": root, "update": {"interpreter": sys.executable, "entryScript": "init.py", "workingDirectory": str(tmp_path)}}
    return c, w, layout


def bhav(path, rows):
    """NSE UDiFF-style equity bhavcopy: rows (symbol, series, o, h, l, c, vol, isin)."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol,ISIN\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return path


# ── repair ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_parse_and_compare_bhavcopy_rows(tmp_path):
    p = bhav(str(tmp_path / "b.csv"), [("AAA", "EQ", 10, 11, 9, 10.5, 1000, "INF1"), ("ZZZ", "SM", 1, 1, 1, 1, 5, "INF9"), ("BBB", "EQ", 5, 6, 4, "-", 10, "INF2")])
    row = rp.parse_bhav_row(p, "aaa")
    assert row == {"series": "EQ", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000.0, "isin": "INF1"}
    assert rp.parse_bhav_row(p, "NOPE") is None and rp.parse_bhav_row(p, "BBB")["close"] is None
    bar = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000.0}
    assert rp.compare(row, bar) == "MATCH" and rp.compare(None, bar) == "ABSENT_IN_SOURCE" and rp.compare(row, None) == "SOURCE_DIFFERS"
    assert rp.compare(row, dict(bar, close=10.6)) == "SOURCE_DIFFERS" and rp.compare(row, dict(bar, volume=999)) == "SOURCE_DIFFERS"
    assert rp.compare(dict(row, close=None), bar) == "SOURCE_DIFFERS"
    assert "Possible split 10:1" in rp.corporate_action_text(100, 10, 1000, 10000) and "Possible split" not in rp.corporate_action_text(100, 99)
    assert "1:" in rp.corporate_action_text(10, 100)
    assert rp.corporate_action_text(None, 5) == "no previous close to compare"


def anomaly_setup(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    i = 200
    rows["aaa"] = rows["aaa"][:i] + [(r[0], r[1] * 1.2, r[2] * 1.2, r[3] * 1.2, r[4] * 1.2, r[5]) for r in rows["aaa"][i:]]
    return w, rows, w.dates[i]


def test_repair_match_then_reexport_shows_the_symbol_unblocked_after_an_approval(tmp_path):
    w, rows, D = anomaly_setup(tmp_path)
    c, _, layout = setup(tmp_path, etf_rows=rows)
    c.eod2_root = layout["dataRoot"] = w.build(etf_rows=rows)
    res = ex.build_snapshot(c.eod2_root, c.registry_path, c.work, now=NOW, existing_roots=[c.work])
    assert "AAA" in res.quarantined and rp.affected_dates(c, "AAA") == [D]
    ops.op_approve_move(c, "AAA", D, "NSE circular")
    bar = next(r for r in rows["aaa"] if r[0] == D)

    def downloader(date, folder):
        assert date == D and folder.replace("\\", "/").endswith("work/repair/" + D)
        return bhav(os.path.join(folder, "bhav.csv") if os.makedirs(folder, exist_ok=True) is None else "", [("AAA", "EQ", bar[1], bar[2], bar[3], bar[4], bar[5], "")])
    before = tree_sig(c.eod2_root)
    rep = rp.repair(c, "AAA", downloader=downloader, publish_kwargs={"live_verify": False})
    assert rep["status"] == "REPAIRED_EXPORT_OK" and rep["comparisons"] == {D: "MATCH"} and any("published" in s for s in rep["steps"])
    assert "AAA" in json.load(open(os.path.join(c.repo, "data", "manifest.json")))["files"]
    assert tree_sig(c.eod2_root) == before                                             # EOD2 untouched
    assert os.path.isfile(os.path.join(c.work, "repair", D, "bhav.csv"))              # raw file kept


def test_repair_source_differs_stops_because_eod2_cannot_replace_a_bar(tmp_path):
    w, rows, D = anomaly_setup(tmp_path)
    c, _, layout = setup(tmp_path, etf_rows=rows)
    c.eod2_root = w.build(etf_rows=rows)
    ex.build_snapshot(c.eod2_root, c.registry_path, c.work, now=NOW, existing_roots=[c.work])
    bar = next(r for r in rows["aaa"] if r[0] == D)

    def downloader(date, folder):
        os.makedirs(folder, exist_ok=True)
        return bhav(os.path.join(folder, "bhav.csv"), [("AAA", "EQ", bar[1], bar[2], bar[3], round(bar[4] / 1.2, 4), bar[5], "")])
    before = tree_sig(c.eod2_root)
    head = sh("git", "-C", c.repo, "rev-parse", "HEAD")
    rep = rp.repair(c, "AAA", downloader=downloader, publish_kwargs={"live_verify": False})
    assert rep["status"] == "BLOCKED_NO_EOD2_ROUTINE" and rep["comparisons"] == {D: "SOURCE_DIFFERS"} and "only appends" in rep["message"]
    assert rep["diagnosis"] and "prev close" in rep["diagnosis"][0]["text"]
    assert tree_sig(c.eod2_root) == before and sh("git", "-C", c.repo, "rev-parse", "HEAD") == head            # nothing edited, nothing published
    assert json.load(open(os.path.join(c.work, "repair", "AAA_report.json")))["status"] == "BLOCKED_NO_EOD2_ROUTINE"


def test_repair_identity_mismatch_and_download_failures(tmp_path):
    w, rows, D = anomaly_setup(tmp_path)
    c, _, _ = setup(tmp_path, etf_rows=rows)
    c.eod2_root = w.build(etf_rows=rows)
    ex.build_snapshot(c.eod2_root, c.registry_path, c.work, now=NOW, existing_roots=[c.work])
    dl = lambda series: (lambda d, f: bhav(os.path.join(os.makedirs(f, exist_ok=True) or f, "b.csv"), [("AAA", series, 1, 1, 1, 1, 1, "")]))  # noqa: E731
    rep = rp.repair(c, "AAA", downloader=dl("BE"), do_publish=False)
    assert rep["status"] != "IDENTITY_MISMATCH"                                        # BE is an acceptable ETF series
    reg = json.load(open(c.registry_path))
    reg["instruments"][0]["isin"] = "INF204KB14I2"
    json.dump(reg, open(c.registry_path, "w"))
    rep = rp.repair(c, "AAA", downloader=lambda d, f: bhav(os.path.join(os.makedirs(f, exist_ok=True) or f, "b.csv"), [("AAA", "EQ", 1, 1, 1, 1, 1, "INF999999999")]))
    assert rep["status"] == "IDENTITY_MISMATCH" and "INF999999999" in rep["message"]

    def boom(d, f):
        raise OSError("no network")
    assert rp.repair(c, "AAA", downloader=boom, strict=True)["status"] == "ERROR"
    rep2 = rp.repair(c, "AAA", downloader=boom, strict=False, do_publish=False)
    assert rep2["comparisons"][D].startswith("DOWNLOAD_FAILED") and rep2["status"] in ("STILL_BLOCKED", "REPAIRED_EXPORT_OK")
    assert rp.repair(c, "NOPE")["status"] == "ERROR"


def test_repair_with_a_range_and_no_downloader_only_reexports(tmp_path):
    c, w, _ = setup(tmp_path)
    assert rp.affected_dates(c, "AAA", w.dates[10], w.dates[12]) == w.dates[10:13]
    rep = rp.repair(c, "AAA", from_date=w.dates[10], to_date=w.dates[12], do_publish=False)
    assert rep["status"] == "REPAIRED_EXPORT_OK" and rep["steps"][0].startswith("exported ")
    assert rp.repair(c, "BBB", do_export=False)["status"] == "NO_CHANGE_NEEDED"


def test_eod2_downloader_runs_eods_own_function_into_the_work_folder(tmp_path, monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"], calls["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout="log line\nC:\\x\\bhav.csv\n", stderr="")
    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    layout = {"update": {"interpreter": "python", "workingDirectory": str(tmp_path)}}
    out = rp.make_eod2_downloader(layout)("2026-01-30", str(tmp_path / "work" / "repair" / "2026-01-30"))
    assert out == "C:\\x\\bhav.csv" and calls["cmd"][0] == "python" and calls["cmd"][1] == "-c" and "equityBhavcopy" in calls["cmd"][2]
    assert calls["cmd"][3].endswith("2026-01-30") and calls["cmd"][4] == "2026-01-30" and calls["kw"]["cwd"] == str(tmp_path)
    monkeypatch.setattr(rp.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403"))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        rp.make_eod2_downloader(layout)("2026-01-30", str(tmp_path / "x"))


# ── run pipeline ───────────────────────────────────────────────────────────────────────────────────────────────────────
def run_kw(tmp_path, **extra):
    return dict(now=NOW, verbose=lambda *a: None, publish_kwargs={"live_verify": False}, notify_kwargs={"secrets_path": str(tmp_path / "no_secrets.json"), "log_dir": str(tmp_path / "logs")}, **extra)


def test_run_success_publishes_and_notify_skips_without_secrets(tmp_path):
    c, w, layout = setup(tmp_path)
    log = []
    rc = runner.run(c, layout, sync_fn=lambda l, now=None: ("SKIPPED", "synced") if log.append("sync") is None else None, **run_kw(tmp_path))
    assert rc == 0 and log == ["sync"]
    st = pub.read_status(c.repo)
    assert st["result"] == "PUBLISHED" and st["datasetId"] == w.latest + "-r1"
    assert os.path.isdir(os.path.join(c.repo, "data", "snapshots", st["datasetId"])) and not os.path.exists(str(tmp_path / "eod2src"))
    assert "no secrets" in open(tmp_path / "logs" / "notify.log", encoding="utf-8").read()
    assert not os.path.exists(os.path.join(c.repo, "work", "dataops.lock"))


def test_run_only_if_needed_skips_when_todays_session_is_published(tmp_path):
    c, w, layout = setup(tmp_path)
    kw = run_kw(tmp_path)
    kw["now"] = __import__("datetime").datetime.fromisoformat("2026-03-11T00:30:00+05:30")     # after midnight: expected session = 2026-03-10?
    assert runner.run(c, layout, sync_fn=lambda l, now=None: ("SKIPPED", "x"), **kw) == 0
    calls = []
    rc = runner.run(c, layout, only_if_needed=True, sync_fn=lambda l, now=None: calls.append(1) or ("SKIPPED", "x"), **kw)
    done, want = runner.already_published(c, {}, kw["now"])
    assert (rc, calls) == (0, []) and done and want <= w.latest
    st = pub.read_status(c.repo)
    st["latestCompletedSession"] = "2000-01-01"
    pub.write_status(c.repo, st)
    assert runner.run(c, layout, only_if_needed=True, sync_fn=lambda l, now=None: calls.append(2) or ("SKIPPED", "x"), **kw) == 0 and calls == [2]


def test_run_sync_failure_records_fail_and_stops(tmp_path):
    c, w, layout = setup(tmp_path)

    def bad(l, now=None):
        raise sy.SyncFail("EOD2 update exited 1")
    seen = []
    kw = run_kw(tmp_path)
    kw["verbose"] = seen.append
    assert runner.run(c, layout, sync_fn=bad, **kw) == 1
    st = pub.read_status(c.repo)
    assert st["result"] == "FAIL" and "sync: EOD2 update exited 1" in st["message"]
    assert not os.path.isdir(os.path.join(c.repo, "data", "snapshots", w.latest + "-r1"))
    assert any("sync:" in str(s) for s in seen)


def test_run_records_unpublishable_when_no_calendar_can_be_built(tmp_path):
    w = World(tmp_path)
    idx = {k: [(r[0], None, None, None, None, None) for r in v] for k, v in w.idx_rows.items()}
    c, _, layout = setup(tmp_path, idx_rows=idx)
    assert runner.run(c, layout, sync_fn=lambda l, now=None: ("SKIPPED", "x"), **run_kw(tmp_path)) == 1
    assert pub.read_status(c.repo)["result"] == "UNPUBLISHABLE"


def test_run_publish_failure_is_reported_and_exit_1(tmp_path):
    c, w, layout = setup(tmp_path)
    sh("git", "-C", c.repo, "remote", "set-url", "origin", str(tmp_path / "does_not_exist.git"))
    assert runner.run(c, layout, sync_fn=lambda l, now=None: ("SKIPPED", "x"), **run_kw(tmp_path)) == 1
    assert pub.read_status(c.repo)["result"] == "FAIL"


def test_run_is_busy_75_and_nests_under_an_outer_lock(tmp_path, isolated_lock):
    import time
    c, w, layout = setup(tmp_path)
    holder = subprocess.Popen([sys.executable, "-m", "dataops", "lock-run", "--", sys.executable, "-c", "import time; time.sleep(10)"], cwd=SRC, env=child_env(isolated_lock))
    try:
        for _ in range(100):
            if os.path.exists(isolated_lock):
                break
            time.sleep(0.1)
        assert runner.run(c, layout, sync_fn=lambda l, now=None: pytest.fail("must not sync"), **run_kw(tmp_path)) == 75
    finally:
        holder.kill()
        holder.wait()
    with lk.DataOpsLock("outer"):                                                   # `run` -> export reuse the lock (no LockBusy)
        assert runner.run(c, layout, sync_fn=lambda l, now=None: ("SKIPPED", "x"), **run_kw(tmp_path)) == 0


# ── CLI in a child process ────────────────────────────────────────────────────────────────────────────────────────────
def cli(tmp_path, isolated_lock, args, c, layout_path=None, extra_env=None):
    env = child_env(isolated_lock, DATAOPS_REPO=c.repo, DATAOPS_EOD2_ROOT=c.eod2_root, DATAOPS_V2_OUT=c.v2_out, DATAOPS_V2_BACKUP=c.v2_backup, DATAOPS_NO_REACQUIRE="1")
    if layout_path:
        env["DATAOPS_LAYOUT"] = layout_path
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "dataops"] + args, cwd=SRC, env=env, capture_output=True, text=True)


def test_cli_registry_commands_end_to_end(tmp_path, isolated_lock):
    c, w, layout = setup(tmp_path)
    r = cli(tmp_path, isolated_lock, ["set-engines", "AAA", "LIFO,TENET"], c)
    assert r.returncode == 0 and "LIFO,TENET" in r.stdout
    assert next(i for i in json.load(open(c.registry_path))["instruments"] if i["symbol"] == "AAA")["engines"] == ["LIFO", "TENET"]
    assert json.load(open(c.v2_out))["etfs"] == ["AAA", "BBB", "CCC"]
    r = cli(tmp_path, isolated_lock, ["remove", "BBB"], c)
    assert r.returncode == 0 and json.load(open(c.v2_out))["etfs"] == ["AAA", "CCC"]
    r = cli(tmp_path, isolated_lock, ["set-listing", "AAA", "2020-01-01", "--evidence", " "], c)
    assert r.returncode == 2 and "refused" in r.stderr and "evidence" in r.stderr
    r = cli(tmp_path, isolated_lock, ["add", "GHOST"], c)
    assert r.returncode == 2 and "NOT_IN_EOD2" in r.stderr
    r = cli(tmp_path, isolated_lock, ["backfill", "CCC", "--from", "2025-03-03"], c)
    assert r.returncode == 0 and "historyStart = 2025-03-03" in r.stdout
    r = cli(tmp_path, isolated_lock, ["add-split", "AAA", "2025-09-01", "10", "--evidence", "x"], c)
    assert r.returncode == 2 and "ratio" in r.stderr
    r = cli(tmp_path, isolated_lock, ["frobnicate"], c)
    assert r.returncode == 2 and "unknown command" in r.stderr and "usage" in r.stderr


def test_cli_approve_move_repairs_and_publishes_and_status_verify_publish(tmp_path, isolated_lock):
    w, rows, D = anomaly_setup(tmp_path)
    c, _, layout = setup(tmp_path, etf_rows=rows)
    c.eod2_root = w.build(etf_rows=rows)
    ex.build_snapshot(c.eod2_root, c.registry_path, c.work, now=NOW, existing_roots=[c.work])
    r = cli(tmp_path, isolated_lock, ["status"], c)
    assert r.returncode == 0 and "no status.json yet" in r.stdout
    r = cli(tmp_path, isolated_lock, ["publish", "--id", "1999-01-01-r1", "--no-verify"], c)
    assert r.returncode == 3 and "refused" in r.stderr
    layout_path = str(tmp_path / "layout.json")
    json.dump({"eod2Src": str(tmp_path), "dataRoot": c.eod2_root, "update": {"interpreter": sys.executable, "entryScript": "x.py", "workingDirectory": str(tmp_path)}}, open(layout_path, "w"))
    r = cli(tmp_path, isolated_lock, ["approve-move", "AAA", D, "--evidence", "NSE circular"], c, layout_path)
    assert r.returncode == 0, r.stderr
    assert "approved move AAA" in r.stdout and "REPAIR AAA" in r.stdout
    reg = json.load(open(c.registry_path))
    assert reg["genuineMoves"][-1]["provenance"] == "dataops"


def test_cli_publish_and_status_and_rebuild_missing(tmp_path, isolated_lock):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    missing = w.dates[200]
    rows["bbb"] = [r for r in rows["bbb"] if r[0] != missing]
    c, _, layout = setup(tmp_path, etf_rows=rows)
    layout_path = str(tmp_path / "layout.json")
    json.dump({"eod2Src": str(tmp_path), "dataRoot": c.eod2_root, "update": {"interpreter": sys.executable, "entryScript": "x.py", "workingDirectory": str(tmp_path)}}, open(layout_path, "w"))
    r = cli(tmp_path, isolated_lock, ["export", "--eod2-data-root", c.eod2_root, "--config", c.registry_path, "--out", c.work], c)
    assert r.returncode == 0 and "BBB" in r.stdout and "MISSING_EXPECTED_SESSION" in r.stdout
    r = cli(tmp_path, isolated_lock, ["publish", "--no-verify"], c)
    assert r.returncode == 0, r.stderr
    st = json.loads(cli(tmp_path, isolated_lock, ["status"], c).stdout)
    assert st["result"] == "PUBLISHED" and st["summary"]["excluded"] == 1
    r = cli(tmp_path, isolated_lock, ["rebuild-missing"], c, layout_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "1 instrument(s) attempted" in r.stdout and "REPAIR BBB" in r.stdout and "new export" in r.stdout


def test_cli_verify_reports_unreachable_live_site(tmp_path, isolated_lock):
    c, _, _ = setup(tmp_path)
    r = cli(tmp_path, isolated_lock, ["verify", "--base-url", "http://127.0.0.1:9/"], c)
    assert r.returncode == 1 and '"ok": false' in r.stdout


def test_cli_sync_fails_cleanly_without_the_eod2_patch(tmp_path, isolated_lock):
    c, _, layout = setup(tmp_path)
    src = tmp_path / "src"
    (src / "defs").mkdir(parents=True)
    (src / "defs" / "defs.py").write_text("dq = int(dq)\n")
    layout_path = str(tmp_path / "layout.json")
    json.dump({"eod2Src": str(src), "dataRoot": c.eod2_root, "update": {"interpreter": sys.executable, "entryScript": "init.py", "workingDirectory": str(src)}}, open(layout_path, "w"))
    r = cli(tmp_path, isolated_lock, ["sync"], c, layout_path)
    assert r.returncode == 1 and "EOD2 local patch missing" in r.stderr
