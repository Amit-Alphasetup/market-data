"""D2: registry operations (add/remove/set-engines/backfill/set-listing/approve-move/add-split) and sync."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from dataops import gen_v2 as g2
from dataops import lock as lk
from dataops import ops
from dataops import schema_universe as su
from dataops import sync as sy
from eod2_factory import World, make_eod2, make_registry, walk, weekdays, write_registry

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 3, 10, 19, 41, 7, tzinfo=IST)


@pytest.fixture
def env(tmp_path):
    w = World(tmp_path)
    root = w.build()
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    write_registry(repo / "config", make_registry(), "universe.json")
    extra = {"zzz": walk(w.dates, 50)}
    open(os.path.join(root, "daily", "zzz.csv"), "w").write(open(os.path.join(root, "daily", "aaa.csv")).read())
    c = ops.Ctx(repo=str(repo), eod2_root=root, v2_out=str(tmp_path / "v2.json"), v2_backup=str(tmp_path / "v2.bak.json"), now=NOW)
    return c, w


def reg(c):
    return json.load(open(c.registry_path, encoding="utf-8"))


def v2(c):
    return json.load(open(c.v2_out, encoding="utf-8"))


def assert_consistent(c):
    r = reg(c)
    assert su.validate(r) == []
    assert v2(c) == g2.gen_v2(r)                       # the v2 config always follows the registry


def test_add_resolves_in_eod2_and_regenerates_v2(env):
    c, _ = env
    assert "added ZZZ" in ops.op_add(c, "zzz", engines="LIFO,DM")
    x = next(i for i in reg(c)["instruments"] if i["symbol"] == "ZZZ")
    assert x["engines"] == ["LIFO", "DM"] and x["enabled"] and x["kind"] == "ETF" and x["assetClass"] == "equity"
    assert "ZZZ" in v2(c)["etfs"]
    assert_consistent(c)
    with pytest.raises(ops.OpError, match="already in the registry"):
        ops.op_add(c, "ZZZ")


def test_add_refuses_symbols_eod2_does_not_have_and_bad_input(env):
    c, _ = env
    with pytest.raises(ops.OpError, match="NOT_IN_EOD2.*newly listed wait for next sync"):
        ops.op_add(c, "NEWLISTED")
    for bad in ("x", "bad symbol", "A" * 30):
        with pytest.raises(ops.OpError):
            ops.op_add(c, bad)
    with pytest.raises(ops.OpError, match="only kind ETF"):
        ops.op_add(c, "ZZZ", kind="INDEX")
    with pytest.raises(ops.OpError, match="engines"):
        ops.op_add(c, "ZZZ", engines="LIFO,FOO")
    with pytest.raises(ops.OpError, match="asset class"):
        ops.op_add(c, "ZZZ", asset_class="crypto")
    assert "ZZZ" not in [i["symbol"] for i in reg(c)["instruments"]]


def test_add_uses_the_asset_class_table_and_isin(env):
    c, _ = env
    open(os.path.join(c.eod2_root, "daily", "goldbees.csv"), "w").write(open(os.path.join(c.eod2_root, "daily", "aaa.csv")).read())
    ops.op_add(c, "GOLDBEES", isin="INF204KB17I5")
    x = next(i for i in reg(c)["instruments"] if i["symbol"] == "GOLDBEES")
    assert x["assetClass"] == "gold" and x["isin"] == "INF204KB17I5"


def test_remove_disables_but_keeps_approvals_and_readd_reenables(env):
    c, _ = env
    ops.op_approve_move(c, "AAA", "2025-09-01", "NSE circular 1")
    assert "disabled AAA" in ops.op_remove(c, "aaa")
    assert "AAA" not in v2(c)["etfs"] and any(m["symbol"] == "AAA" for m in reg(c)["genuineMoves"])
    assert "already disabled" in ops.op_remove(c, "AAA")
    assert "re-enabled" in ops.op_add(c, "AAA")
    assert "AAA" in v2(c)["etfs"]
    with pytest.raises(ops.OpError, match="not in the registry"):
        ops.op_remove(c, "NOPE")
    assert_consistent(c)


def test_set_engines_subset_and_data_only(env):
    c, _ = env
    assert "LIFO,TENET" in ops.op_set_engines(c, "AAA", "lifo, tenet")
    assert next(i for i in reg(c)["instruments"] if i["symbol"] == "AAA")["engines"] == ["LIFO", "TENET"]
    assert "data-only" in ops.op_set_engines(c, "AAA", "")
    for bad in ("LIFO,LIFO", "FOO", "DM,,X"):
        with pytest.raises(ops.OpError):
            ops.op_set_engines(c, "AAA", bad)
    assert_consistent(c)


def test_backfill_sets_history_start_and_reports_the_real_first_bar(env):
    c, w = env
    msg = ops.op_backfill(c, "AAA", "2024-01-01")
    assert "historyStart = 2024-01-01" in msg and "actual first bar is %s" % w.dates[0] in msg and "LATER than requested" in msg
    msg2 = ops.op_backfill(c, "BBB", "2025-03-03")
    assert "LATER" not in msg2 and next(i for i in reg(c)["instruments"] if i["symbol"] == "BBB")["historyStart"] == "2025-03-03"
    assert v2(c)["trim_before"]["BBB"] == "2025-03-03"
    with pytest.raises(ops.OpError, match="real YYYY-MM-DD"):
        ops.op_backfill(c, "AAA", "2025-02-30")
    assert_consistent(c)


def test_set_listing_needs_evidence(env):
    c, _ = env
    for ev in ("", "   ", None):
        with pytest.raises(ops.OpError, match="evidence"):
            ops.op_set_listing(c, "AAA", "2020-01-01", ev)
    with pytest.raises(ops.OpError, match="date"):
        ops.op_set_listing(c, "AAA", "soon", "NSE")
    ops.op_set_listing(c, "AAA", "2020-01-01", "  NSE listing circular  ")
    x = next(i for i in reg(c)["instruments"] if i["symbol"] == "AAA")
    assert x["listingDate"] == "2020-01-01" and x["listingDateEvidence"] == "NSE listing circular"
    assert_consistent(c)


def test_approve_move_records_evidence_provenance_and_time(env):
    c, _ = env
    with pytest.raises(ops.OpError, match="evidence"):
        ops.op_approve_move(c, "AAA", "2025-09-01", "")
    ops.op_approve_move(c, "AAA", "2025-09-01", "NSE circular 42")
    m = reg(c)["genuineMoves"][-1]
    assert m == {"symbol": "AAA", "date": "2025-09-01", "evidence": "NSE circular 42", "approvedAt": "2026-03-10T19:41:07+05:30", "provenance": "dataops"}
    assert v2(c)["genuine"]["AAA|2025-09-01"] == "NSE circular 42"
    with pytest.raises(ops.OpError, match="already exists"):
        ops.op_approve_move(c, "AAA", "2025-09-01", "again")
    ops.op_approve_move(c, "AAA", "2025-09-02", "another date is a separate approval")
    assert_consistent(c)


def test_add_split_parses_ratio_and_records_it(env):
    c, _ = env
    for bad in ("10", "0:1", "a:b", "10:0", "-2:1"):
        with pytest.raises(ops.OpError):
            ops.op_add_split(c, "AAA", "2025-09-01", bad, "ev")
    with pytest.raises(ops.OpError, match="evidence"):
        ops.op_add_split(c, "AAA", "2025-09-01", "10:1", " ")
    ops.op_add_split(c, "AAA", "2025-09-01", "3:2", "NSE corporate action ref")
    s = reg(c)["splits"][-1]
    assert (s["ratioNum"], s["ratioDen"], s["provenance"], s["evidence"]) == (3, 2, "dataops", "NSE corporate action ref")
    assert v2(c)["splits"][-1] == ["AAA", "2025-09-01", 1.5]
    with pytest.raises(ops.OpError, match="already exists"):
        ops.op_add_split(c, "AAA", "2025-09-01", "10:1", "x")
    assert_consistent(c)


# ── sync ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_expected_latest_session():
    hol = {"2026-03-09": "x"}                                                # Monday holiday
    at = lambda s: datetime.fromisoformat(s + "+05:30")  # noqa: E731
    assert sy.expected_latest_session(at("2026-03-10T19:14:59"), hol) == "2026-03-06"      # Tue before cutoff: Mon is a holiday -> Fri
    assert sy.expected_latest_session(at("2026-03-10T19:15:00"), hol) == "2026-03-10"      # at the cutoff today counts
    assert sy.expected_latest_session(at("2026-03-14T23:00:00"), hol) == "2026-03-13"      # Saturday -> Friday
    assert sy.expected_latest_session(at("2026-03-15T12:00:00"), hol) == "2026-03-13"      # Sunday
    assert sy.expected_latest_session(at("2026-03-10T15:00:00").astimezone(timezone.utc), {}) == "2026-03-09"   # timezone-safe
    assert sy.expected_latest_session(at("2026-03-10T20:00:00"), {"2026-03-10": "h"}) == "2026-03-09"


def fake_layout(tmp_path, latest="2026-03-06", patched=True, interpreter=sys.executable, script="init.py"):
    src = tmp_path / "eod2src"
    (src / "defs").mkdir(parents=True)
    (src / "defs" / "defs.py").write_text("dq = t.V if x else (int(dq) if str(dq).strip().isdigit() else 0)\n" if patched else "dq = int(dq)\n", encoding="utf-8")
    root = make_eod2(tmp_path, {}, {"nifty 50": []}, last_update=latest, holidays={})
    (src / script).write_text("print('updating')\n", encoding="utf-8")
    patch = tmp_path / "patch.txt"
    patch.write_text("(int(dq) if str(dq).strip().isdigit() else 0)\n", encoding="utf-8")
    return {"eod2Src": str(src), "dataRoot": root, "update": {"interpreter": interpreter, "entryScript": script, "workingDirectory": str(src)}}, str(patch)


def test_check_patch(tmp_path):
    lay, patch = fake_layout(tmp_path)
    sy.check_patch(lay["eod2Src"], patch)
    lay2, patch2 = fake_layout(tmp_path / "b", patched=False) if (tmp_path / "b").mkdir() is None else (None, None)
    with pytest.raises(sy.SyncFail, match="EOD2 local patch missing — re-apply"):
        sy.check_patch(lay2["eod2Src"], patch2)
    with pytest.raises(sy.SyncFail, match="eod2_patch.txt is missing"):
        sy.check_patch(lay["eod2Src"], str(tmp_path / "nope.txt"))


def test_real_eod2_has_the_local_patch():
    src = r"C:\dev\eod2\src"
    if not os.path.isdir(src):
        pytest.skip("EOD2 not installed here")
    sy.check_patch(src)                                                          # config/eod2_patch.txt is present in the real defs.py


def test_sync_skips_when_already_synced_and_does_not_start_eod2(tmp_path):
    lay, patch = fake_layout(tmp_path, latest="2026-03-10")
    called = []
    st, msg = sy.sync(lay, now=NOW, runner=lambda *a: called.append(a) or 0, running_check=lambda: [], log_dir=str(tmp_path / "logs"), patch_file=patch)
    assert st == "SKIPPED" and "already synced through 2026-03-10" in msg and not called


def test_sync_runs_eod2_update_logs_and_reports_the_new_last_update(tmp_path):
    lay, patch = fake_layout(tmp_path, latest="2026-03-06")
    seen = {}

    def runner(cmd, cwd, log):
        seen["cmd"], seen["cwd"] = cmd, cwd
        log.write(b"eod2 says hi\n")
        meta = os.path.join(lay["dataRoot"], "meta.json")
        m = json.load(open(meta))
        m["lastUpdate"] = "2026-03-10T00:00:00+05:30"
        json.dump(m, open(meta, "w"))
        return 0
    st, msg = sy.sync(lay, now=NOW, runner=runner, running_check=lambda: [], log_dir=str(tmp_path / "logs"), patch_file=patch)
    assert st == "SYNCED" and "2026-03-06 -> 2026-03-10" in msg
    assert seen["cmd"] == [sys.executable, "init.py"] and seen["cwd"] == lay["eod2Src"]
    log = open(tmp_path / "logs" / "eod2_20260310.log", encoding="utf-8").read()
    assert "eod2 says hi" in log and "expected 2026-03-10, had 2026-03-06" in log


def test_sync_fails_on_nonzero_exit_and_on_missing_patch(tmp_path):
    lay, patch = fake_layout(tmp_path)
    with pytest.raises(sy.SyncFail, match="exited 1"):
        sy.sync(lay, now=NOW, runner=lambda *a: 1, running_check=lambda: [], log_dir=str(tmp_path / "logs"), patch_file=patch)
    (tmp_path / "x").mkdir()
    lay2, patch2 = fake_layout(tmp_path / "x", patched=False)
    with pytest.raises(sy.SyncFail, match="patch missing"):
        sy.sync(lay2, now=NOW, runner=lambda *a: 0, running_check=lambda: [], log_dir=str(tmp_path / "logs"), patch_file=patch2)


def test_sync_is_busy_while_the_legacy_job_runs(tmp_path):
    lay, patch = fake_layout(tmp_path)
    with pytest.raises(lk.LockBusy) as e:
        sy.sync(lay, now=NOW, runner=lambda *a: pytest.fail("must not start"), running_check=lambda: ["powershell.exe (pid 42)"], log_dir=str(tmp_path / "logs"), patch_file=patch)
    assert e.value.exit_code == 75 and "EOD2 update already running" in str(e.value)


def test_legacy_job_detection_sees_a_process_by_command_line():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)  # publish_data.ps1"])
    try:
        import time
        time.sleep(0.5)
        hits = sy.legacy_job_running()
        assert any("pid %d" % p.pid in h for h in hits), hits
    finally:
        p.kill()
        p.wait()
    assert not any("pid %d" % p.pid in h for h in sy.legacy_job_running())
