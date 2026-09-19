"""D1 unit tests: layout discovery, hashing vs the A0 fixtures, EOD2 reader, calendar, export CLI."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from conftest import FIXTURES, child_env
from dataops import calendar as cal
from dataops import eod2_reader as er
from dataops import hashing as H
from dataops import layout as lay
from eod2_factory import World, make_eod2, make_registry, write_registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


# ── layout ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def fake_eod2_src(tmp_path, meta=None, init_extra="", defs_extra=""):
    src = tmp_path / "eod2" / "src"
    (src / "defs").mkdir(parents=True)
    data = src / "eod2_data"
    (data / "daily").mkdir(parents=True)
    (data / "daily" / "nifty 50.csv").write_text("Date,Open,High,Low,Close\n2026-01-01,1,1,1,1\n", encoding="utf-8")
    (data / "meta.json").write_text(json.dumps(meta or {"lastUpdate": "2026-01-01T00:00:00+05:30", "holidays": {}, "year": 2026, "special_sessions": []}), encoding="utf-8")
    (data / "isin_symbol_map.json").write_text('{"sym2isin": {}}', encoding="utf-8")
    (src / "init.py").write_text("BHAV = nse.equityBhavcopy(defs.dates.dt)\nI = nse.indicesBhavcopy(defs.dates.dt)\nD = nse.deliveryBhavcopy(defs.dates.dt)\n" + init_extra, encoding="utf-8")
    (src / "defs" / "defs.py").write_text("def updateNseEOD(a, b):\n    pass\ndef updateIndexEOD(f):\n    pass\ndef adjustNseStocks():\n    pass\ndef rollback(f):\n    pass\n"
                                          "def updateNseSymbol(symFile):\n    with symFile.open(\"ab\") as f:\n        pass\n" + defs_extra, encoding="utf-8")
    return str(src)


def test_discover_layout_records_the_facts(tmp_path):
    L = lay.discover_layout(fake_eod2_src(tmp_path))
    assert L["dailyFolder"].endswith("eod2_data\\daily") and L["meta"]["lastSyncKey"] == "lastUpdate" and L["meta"]["holidaysKey"] == "holidays"
    assert L["isin"]["eod2StoresIsin"] is True and L["update"]["entryCommand"] == "python init.py"
    assert L["singleDate"]["downloadFunctions"]["equity"] == "NSE.equityBhavcopy(date)" and L["singleDate"]["updateAppendsOnly"] is True
    assert "APPENDS" in L["singleDate"]["updateNote"] and L["sessionCalendarIndex"] == "NIFTY 50"


@pytest.mark.parametrize("what", ["daily", "meta", "metakeys", "download", "func"])
def test_discover_layout_refuses_to_guess(tmp_path, what):
    src = fake_eod2_src(tmp_path)
    data = os.path.join(src, "eod2_data")
    if what == "daily":
        os.remove(os.path.join(data, "daily", "nifty 50.csv"))
    elif what == "meta":
        os.remove(os.path.join(data, "meta.json"))
    elif what == "metakeys":
        json.dump({"foo": 1}, open(os.path.join(data, "meta.json"), "w"))
    elif what == "download":
        open(os.path.join(src, "init.py"), "w").write("pass\n")
    elif what == "func":
        open(os.path.join(src, "defs", "defs.py"), "w").write("def updateNseEOD(a, b):\n    pass\n")
    with pytest.raises(lay.LayoutError):
        lay.discover_layout(src)


def test_committed_real_layout_is_consistent():
    p = os.path.join(ROOT, "config", "eod2_layout.json")
    assert os.path.isfile(p)
    L = json.load(open(p, encoding="utf-8"))
    assert L["layoutVersion"] == 1 and L["meta"]["holidaysYearKey"] == "year" and L["singleDate"]["updateAppendsOnly"] is True
    assert L["dailyFolder"].startswith(L["dataRoot"]) and L["isin"]["eod2StoresIsin"] is True
    if os.path.isdir(L["dailyFolder"]):
        assert os.path.isfile(os.path.join(L["dailyFolder"], "nifty 50.csv")) and os.path.isfile(L["meta"]["path"])


# ── hashing (independent of the fixture generator's implementation) ──────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["snap_basic", "snap_b", "snap_special"])
def test_hashing_reproduces_the_a0_fixture_manifests(name):
    base = os.path.join(FIXTURES, name, "data")
    if not os.path.isdir(base):
        pytest.skip("fixtures missing")
    m = json.load(open(os.path.join(base, "manifest.json"), encoding="utf-8"))
    sdir = os.path.join(base, m["snapshotBase"])
    actual = {k: {"sha256": H.sha256_file(os.path.join(sdir, f["path"]))} for k, f in m["files"].items()}
    assert all(actual[k]["sha256"] == m["files"][k]["sha256"] for k in actual)
    cal_sha = H.sha256_file(os.path.join(sdir, "calendar.json"))
    assert cal_sha == m["calendar"]["sha256"]
    pol = H.policy_hash("eod2-split-adjusted-price-only", [], [], [], {})
    assert pol == m["policyHash"]
    assert H.dataset_hash(actual, cal_sha, pol) == m["datasetHash"]


def test_canonical_json_and_dataset_hash_rules():
    assert H.canonical_json({"b": 1, "a": [1, 2], "é": "x"}) == '{"a":[1,2],"b":1,"é":"x"}'.encode("utf-8")
    assert H.policy_hash("p", [], [], {}, {}) != H.policy_hash("p", [], [], {"X": "y"}, {})
    files = {"B": {"sha256": "b" * 64}, "A": {"sha256": "a" * 64}}
    expect = hashlib.sha256(("file:A:%s\nfile:B:%s\ncalendar:%s\npolicy:%s\ncontract:alphadesk-eod2/3\n" % ("a" * 64, "b" * 64, "c" * 64, "d" * 64)).encode()).hexdigest()
    assert H.dataset_hash(files, "c" * 64, "d" * 64) == expect                       # sorted, newline-terminated, contract line last
    assert H.sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()


# ── reader ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_reader_parses_like_v2_and_reports_junk(tmp_path):
    root = make_eod2(tmp_path, {"aaa": [("2025-01-02", 1, 2, 1, 2, 10), ("2025-01-01", 1, 2, 1, 1.5, 5), ("2025-01-03", None, None, None, 2.5, 7)]}, {"nifty 50": []})
    open(os.path.join(root, "daily", "bad.csv"), "w", encoding="utf-8").write(
        "\ufeffDate,Open,High,Low,Close,Volume,Series\n2025-01-01,1,2,1,2,10,EQ\n2025-01-01,1,2,1,2,10,EQ\n2025-02-30,1,2,1,2,10,EQ\n\n2025-01-05,1,2,1,Infinity,10,EQ\n2025-01-06,1,2,1,abc,10,EQ\n")
    src = er.Eod2Source(root)
    rows, f = src.read_bars("aaa.csv")
    assert [r["date"] for r in rows] == ["2025-01-01", "2025-01-02", "2025-01-03"] and f == []          # sorted like v2
    assert rows[2]["open"] is None and rows[2]["close"] == 2.5 and rows[0]["series"] == "EQ"
    rows, f = src.read_bars("bad.csv")
    assert {x["code"] for x in f} == {"SCHEMA_INVALID", "INVALID_DATE"} and any(x.get("date") == "2025-02-30" for x in f)
    assert rows[1]["close"] is er.BAD and rows[2]["close"] is er.BAD                                    # BOM handled, junk flagged
    assert er.fnum("") is None and er.fnum(None) is None and er.fnum("1e3") == 1000.0 and er.fnum("nan") is er.BAD and er.fnum("-inf") is er.BAD


def test_reader_meta_holidays_special_sessions_and_isin(tmp_path):
    root = make_eod2(tmp_path, {}, {"nifty 50": []}, holidays={"26-Jan-2026": "Republic Day", "01-May-2026": "Maharashtra Day", "bogus": "x"},
                     special_meta=["2026-02-01T00:00:00"], special_txt=["2024-03-02", "not a date", ""], isin={"AAA": "INF204KB14I2"})
    src = er.Eod2Source(root)
    hol, year = src.holidays()
    assert hol == {"2026-01-26": "Republic Day", "2026-05-01": "Maharashtra Day"} and year == 2026
    assert src.special_sessions() == {"2026-02-01", "2024-03-02"} and src.last_update() == "2026-03-10"
    assert src.isin_of("AAA") == "INF204KB14I2" and src.isin_of("ZZZ") is None and src.stores_isin()
    assert er.parse_holiday_key("05-Sep-2026") == "2026-09-05" and er.parse_holiday_key("zz") is None
    with pytest.raises(FileNotFoundError):
        er.Eod2Source(os.path.join(str(tmp_path), "nope"))


def test_stems_are_case_insensitive_and_keep_spaces(tmp_path):
    root = make_eod2(tmp_path, {"AbC": []}, {"nifty 50": [], "nifty midcap 150": []})
    st = er.Eod2Source(root).stems()
    assert "abc" in st and "nifty midcap 150" in st


# ── calendar ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_calendar_build_and_validate():
    dates = ["2025-01-01", "2025-01-02", "2025-01-04", "2025-01-06"]          # 2025-01-04 is a Saturday
    c = cal.build_calendar(dates, {}, 2025, set(), "2025-01-06", "19:15", "2025-01-01")
    assert c["sessions"][:3] == ["2025-01-01", "2025-01-02", "2025-01-06"] and c["specialSessions"] == [{"date": "2025-01-04", "kind": "SPECIAL"}]
    assert c["coverageTo"] == "2025-12-31" and c["verifiedThrough"] == "2025-12-31" and c["sessions"][-1] == "2025-12-31"
    assert cal.validate_calendar(c) == []
    bad = dict(c, sessions=list(reversed(c["sessions"])))
    assert cal.validate_calendar(bad)
    assert cal.validate_calendar(dict(c, sessions=[]))
    assert cal.validate_calendar(dict(c, verifiedThrough="2020-01-01"))
    assert cal.validate_calendar({}) and cal.validate_calendar("x")
    assert json.loads(cal.dumps(c)) == c and " " not in cal.dumps(dict(c, holidays=[]))[:20]
    stale = cal.build_calendar(dates, {}, 2024, set(), "2025-01-06", "19:15", "2025-01-01")     # holiday list is a year behind
    assert stale["verifiedThrough"] == "2025-01-06" and stale["coverageTo"] == "2025-12-31"


def test_latest_session_may_itself_be_a_special_session():
    dates = ["2025-01-31", "2025-02-01"]                                       # Saturday budget session as the newest bar
    c = cal.build_calendar(dates, {}, 2025, {"2025-02-01"}, "2025-02-01", "19:15", "2025-01-01")
    assert cal.validate_calendar(c) == [] and "2025-02-01" not in c["sessions"]


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_export_cli_end_to_end(tmp_path, isolated_lock):
    w = World(tmp_path)
    root = w.build()
    cfg = write_registry(tmp_path, make_registry())
    out = os.path.join(str(tmp_path), "cli_out")
    r = subprocess.run([sys.executable, "-m", "dataops", "export", "--eod2-data-root", root, "--config", cfg, "--out", out],
                       cwd=SRC, env=child_env(isolated_lock), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "PUBLISHABLE" in r.stdout and w.latest in r.stdout and "snapshot folder:" in r.stdout
    assert os.path.isfile(os.path.join(out, w.latest + "-r1", "manifest.json")) and not os.path.exists(isolated_lock)


def test_export_cli_is_refused_while_the_lock_is_held(tmp_path, isolated_lock):
    import time
    w = World(tmp_path)
    root = w.build()
    cfg = write_registry(tmp_path, make_registry())
    holder = subprocess.Popen([sys.executable, "-m", "dataops", "lock-run", "--", sys.executable, "-c", "import time; time.sleep(8)"], cwd=SRC, env=child_env(isolated_lock))
    try:
        for _ in range(100):
            if os.path.exists(isolated_lock):
                break
            time.sleep(0.1)
        r = subprocess.run([sys.executable, "-m", "dataops", "export", "--eod2-data-root", root, "--config", cfg, "--out", os.path.join(str(tmp_path), "o")],
                           cwd=SRC, env=child_env(isolated_lock), capture_output=True, text=True)
        assert r.returncode == 75 and not os.path.exists(os.path.join(str(tmp_path), "o"))
    finally:
        holder.kill()
        holder.wait()


def test_report_lists_the_q11_etfs(tmp_path):
    from dataops import export as ex
    from dataops import report as rp
    from datetime import datetime, timedelta, timezone
    w = World(tmp_path)
    rows = {k: list(v) for k, v in w.etf_rows.items()}
    first = next(i for i, r in enumerate(rows["aaa"]) if r[0] >= "2025-09-01")
    rows["aaa"] = rows["aaa"][first:]
    root = w.build(etf_rows=rows)
    res = ex.build_snapshot(root, write_registry(tmp_path, make_registry()), os.path.join(str(tmp_path), "o"),
                            now=datetime(2026, 3, 10, tzinfo=timezone(timedelta(hours=5, minutes=30))), existing_roots=[])
    text = rp.format_report(res)
    assert "Q11" in text and "AAA" in text and "WARN ISIN_MISSING (3)" in text and "PUBLISHABLE" in text
    assert "UNPUBLISHABLE" in rp.format_report(ex.SnapshotResult())
