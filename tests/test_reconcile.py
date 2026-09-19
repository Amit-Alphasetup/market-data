"""D0.5: reconcile groups + --apply (fixture EOD2 folder, temp registry, temp v2 output, temp lock)."""
import copy
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import FIXTURES, child_env
from dataops import gen_v2 as g2
from dataops import lock as lk
from dataops import migrate_v2 as mig
from dataops import reconcile as rc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "data")
V2 = json.load(open(os.path.join(DATA, "eod2_universe.v2.original.json"), encoding="utf-8-sig"))
APP = json.load(open(os.path.join(DATA, "app_lists_v321.json"), encoding="utf-8"))
EOD2_MIN = os.path.join(FIXTURES, "eod2_min")

pytestmark = pytest.mark.skipif(not os.path.isdir(EOD2_MIN), reason="fixtures missing (run C:\\dev\\fixtures\\make_fixtures.py)")


def small_registry():
    r = mig.migrate(copy.deepcopy(V2), APP["lifo"], APP["mom"])
    keep = {"NIFTYBEES", "MONQ50", "MIDCAPETF"}
    r["instruments"] = [x for x in r["instruments"] if x["symbol"] in keep]
    r["splits"] = [s for s in r["splits"] if s["symbol"] in keep]
    r["genuineMoves"] = [m for m in r["genuineMoves"] if m["symbol"] in keep]
    return r


def legacy_manifest(tmp_path):
    m = {"files": {"NIFTYBEES": {"first": "2019-01-14", "last": "2026-09-18", "bars": 1900}},
         "quarantined": {"MONQ50": ["unexplained +19.8% on 2026-09-16 (ratio 0.83)"]}}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return str(p)


def app_file(tmp_path, lines):
    p = tmp_path / "app.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_read_app_universe_formats(tmp_path):
    p = app_file(tmp_path, ["niftybees,both", "GOLDBEES", "# comment", "", "LIQUIDBEES , Momentum", "niftybees,lifo", "XYZ,weird"])
    assert rc.read_app_universe(p) == [("NIFTYBEES", "both"), ("GOLDBEES", None), ("LIQUIDBEES", "momentum"), ("XYZ", None)]


def test_four_groups_on_fixture(tmp_path):
    reg = small_registry()
    reg["instruments"].append({**copy.deepcopy(reg["instruments"][0]), "id": "NSE:GHOSTETF", "symbol": "GHOSTETF", "name": "GHOSTETF"})
    app = rc.read_app_universe(app_file(tmp_path, ["NIFTYBEES,both", "GOLDBEES,lifo", "LIQUIDBEES,momentum", "NOPEETF,lifo", "bad sym"]))
    g = rc.classify(reg, app, rc.load_manifest(legacy_manifest(tmp_path)), rc.eod2_daily_index(EOD2_MIN))
    a = {x["symbol"]: x for x in g["a"]}
    assert set(a) == {"GOLDBEES", "LIQUIDBEES", "NOPEETF", "BAD SYM"}
    assert a["GOLDBEES"]["eod2"] == "ONE" and a["LIQUIDBEES"]["eod2"] == "ONE" and a["NOPEETF"]["eod2"] == "NONE"
    assert a["BAD SYM"]["validSymbol"] is False
    assert [x["symbol"] for x in g["d"]] == ["NIFTYBEES"]                                          # exported OK
    assert [(x["symbol"], x["events"]) for x in g["c"]] == [("MONQ50", ["unexplained +19.8% on 2026-09-16 (ratio 0.83)"])]
    reasons = {x["symbol"]: x["reason"] for x in g["b"]}
    assert reasons == {"MIDCAPETF": "NOT_IN_EOD2", "GHOSTETF": "NOT_IN_EOD2"}                      # no such csv in the fixture
    text = rc.format_report(g)
    for head in ("(a) IN APP", "(b) IN REGISTRY", "(c) QUARANTINED", "(d) EXPORTED OK"):
        assert head in text


def test_ambiguous_eod2_file_is_reported():
    reg = small_registry()
    g = rc.classify(reg, [("DUPETF", "lifo")], None, {"dupetf": ["dupetf.csv", "DUPETF.csv"]})
    assert g["a"][0]["eod2"] == "AMBIGUOUS"
    added, skipped = rc.add_to_registry(reg, g)
    assert added == [] and skipped == [("DUPETF", "AMBIGUOUS_SYMBOL")]


def test_missing_manifest_is_handled():
    g = rc.classify(small_registry(), [], None, rc.eod2_daily_index(EOD2_MIN))
    reasons = {x["symbol"]: x["reason"] for x in g["b"]}
    assert reasons == {"NIFTYBEES": "NO_LEGACY_MANIFEST", "MONQ50": "NOT_IN_EOD2", "MIDCAPETF": "NOT_IN_EOD2"}
    assert g["c"] == [] and g["d"] == []


def run_main(tmp_path, extra, lines=("GOLDBEES,lifo", "LIQUIDBEES,momentum", "NOPEETF,lifo", "NIFTYBEES,both")):
    reg_path = tmp_path / "universe.json"
    reg_path.write_text(mig.dumps(small_registry()), encoding="utf-8", newline="\n")
    v2_out = tmp_path / "v2.json"
    v2_out.write_text('{"orig": 1}', encoding="utf-8")
    argv = ["--app-universe", app_file(tmp_path, list(lines)), "--registry", str(reg_path), "--manifest", legacy_manifest(tmp_path),
            "--eod2-root", EOD2_MIN, "--v2-out", str(v2_out), "--v2-backup", str(tmp_path / "v2.bak.json")] + extra
    return argv, reg_path, v2_out


def test_report_only_changes_nothing(tmp_path, capsys):
    argv, reg_path, v2_out = run_main(tmp_path, [])
    before = reg_path.read_bytes()
    assert rc.main(argv) == 0
    assert reg_path.read_bytes() == before and v2_out.read_text(encoding="utf-8") == '{"orig": 1}'
    assert "(a) IN APP, NOT IN REGISTRY: 3" in capsys.readouterr().out


def test_apply_adds_only_symbols_with_exactly_one_eod2_file(tmp_path, capsys):
    argv, reg_path, v2_out = run_main(tmp_path, ["--apply"])
    assert rc.main(argv) == 0
    out = capsys.readouterr().out
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    by = {x["symbol"]: x for x in reg["instruments"]}
    assert by["GOLDBEES"]["engines"] == ["LIFO"] and by["GOLDBEES"]["assetClass"] == "gold"
    assert by["LIQUIDBEES"]["engines"] == ["DM"] and by["LIQUIDBEES"]["assetClass"] == "liquid"
    assert "NOPEETF" not in by and "not added: NOPEETF" in out and "NOT_IN_EOD2" in out
    cfg = json.loads(v2_out.read_text(encoding="utf-8"))
    assert "GOLDBEES" in cfg["etfs"] and "LIQUIDBEES" in cfg["etfs"] and "NOPEETF" not in cfg["etfs"]
    assert json.loads((tmp_path / "v2.bak.json").read_text(encoding="utf-8")) == {"orig": 1}
    assert "legacy job NOT started" in out
    from dataops import schema_universe as su
    assert su.validate(reg) == []
    # idempotent: applying again adds nothing
    assert rc.main(argv) == 0
    assert "added 0" in capsys.readouterr().out


def test_apply_is_refused_while_the_lock_is_held_by_another_process(tmp_path, isolated_lock):
    argv, reg_path, v2_out = run_main(tmp_path, ["--apply"])
    before = reg_path.read_bytes()
    holder = subprocess.Popen([sys.executable, "-m", "dataops", "lock-run", "--", sys.executable, "-c", "import time; time.sleep(8)"],
                              env=child_env(isolated_lock), cwd=os.path.join(ROOT, "src"))
    try:
        import time
        for _ in range(100):
            if os.path.exists(isolated_lock):
                break
            time.sleep(0.1)
        assert rc.main(argv) == lk.EXIT_BUSY == 75
        assert reg_path.read_bytes() == before and v2_out.read_text(encoding="utf-8") == '{"orig": 1}'
    finally:
        holder.kill()
        holder.wait()


def test_run_legacy_uses_lock_run_with_the_legacy_command(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(rc.lk, "lock_run", lambda argv, command=None: called.setdefault("argv", argv) and 0)
    argv, _, _ = run_main(tmp_path, ["--apply", "--run-legacy"])
    assert rc.main(argv) == 0
    assert called["argv"] == rc.LEGACY_JOB and called["argv"][-1] == r"C:\dev\publish_data.ps1"
