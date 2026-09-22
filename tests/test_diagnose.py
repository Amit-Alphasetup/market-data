"""D0.5: diagnose is read-only and reports what Apd needs for Q10 (MONQ50)."""
import hashlib
import json
import os
import shutil

import pytest

from dataops import diagnose as dg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_eod2(tmp_path, rows, name="testetf"):
    d = tmp_path / "eod2" / "daily"
    d.mkdir(parents=True)
    hdr = "Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY"
    lines = [hdr] + ["%s,%s,%s,%s,%s,%s,EQ,,," % r for r in rows]
    (d / (name + ".csv")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(tmp_path / "eod2")


ROWS = [("2026-09-11", 100, 101, 99, 100, 1000), ("2026-09-14", 100, 102, 99, 101, 1100), ("2026-09-15", 101, 103, 100, 102, 900),
        ("2026-09-16", 25, 26, 24, 25.5, 4500), ("2026-09-17", 25.5, 26, 25, 25.8, 4000), ("2026-09-18", 25.8, 26.5, 25.5, 26, 3800)]


def registry_file(tmp_path):
    reg = {"splits": [{"symbol": "TESTETF", "date": "2020-01-01", "ratioNum": 10, "ratioDen": 1, "evidence": None, "approvedAt": None, "provenance": "migrated-v2-2026-09-11"}],
           "genuineMoves": [{"symbol": "TESTETF", "date": "2026-09-07", "evidence": None, "approvedAt": None, "provenance": "migrated-v2-2026-09-11", "note": "real print"},
                            {"symbol": "OTHER", "date": "2026-09-07", "evidence": None, "approvedAt": None, "provenance": "migrated-v2-2026-09-11"}],
           "badPrints": [{"symbol": "TESTETF", "date": "2026-09-20", "evidence": "vendor glitch", "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"}]}
    p = tmp_path / "reg.json"
    p.write_text(json.dumps(reg), encoding="utf-8")
    return str(p)


def test_analyse_flags_split_like_move_with_ratio_and_volume(tmp_path):
    root = make_eod2(tmp_path, ROWS)
    text, recs = dg.run("TESTETF", "2026-09-14", "2026-09-18", root, registry_file(tmp_path))
    by = {r["date"]: r for r in recs}
    r = by["2026-09-16"]
    assert r["flag"] and r["ret"] == pytest.approx(25.5 / 102 - 1)
    assert r["ratio"] == pytest.approx(4.0) and r["round"] == 4.0 and r["roundErr"] == pytest.approx(0.0)
    assert r["volRatio"] == pytest.approx(5.0)                       # 4500 / 900
    assert not by["2026-09-14"]["flag"] and by["2026-09-14"]["ret"] == pytest.approx(0.01)   # first row uses the bar before `from`
    assert "ANOMALIES" in text and "nearest round split ratio 4" in text and "volume x5.00" in text
    assert "|ret| >= 15%" in text


def test_existing_approvals_are_listed_with_dates_and_only_for_that_symbol(tmp_path):
    root = make_eod2(tmp_path, ROWS)
    text, _ = dg.run("testetf", "2026-09-14", "2026-09-18", root, registry_file(tmp_path))
    assert "2026-09-07" in text and "provenance=migrated-v2-2026-09-11" in text and "real print" in text and "ratio 10:1" in text
    assert "OTHER" not in text
    assert "2026-09-20" in text and "bad print (dropped)" in text and "vendor glitch" in text
    text2, _ = dg.run("TESTETF", "2026-09-14", "2026-09-18", root, None)   # no registry -> none
    assert "EXISTING APPROVALS for TESTETF" in text2 and "\n  none" in text2


def test_read_only_files_unchanged(tmp_path):
    root = make_eod2(tmp_path, ROWS)
    path = os.path.join(root, "daily", "testetf.csv")
    before = (hashlib.sha256(open(path, "rb").read()).hexdigest(), os.path.getmtime(path))
    dg.run("TESTETF", "2026-09-14", "2026-09-18", root, registry_file(tmp_path))
    after = (hashlib.sha256(open(path, "rb").read()).hexdigest(), os.path.getmtime(path))
    assert before == after


def test_yahoo_column_is_diagnosis_only_and_failures_do_not_break_the_report(tmp_path):
    root = make_eod2(tmp_path, ROWS)
    text, _ = dg.run("TESTETF", "2026-09-14", "2026-09-18", root, registry_file(tmp_path), compare_yahoo=True,
                     yahoo_fn=lambda s, a, b: {"2026-09-16": 25.5, "2026-09-17": 26.0})
    assert "yahoo" in text and "DIAGNOSIS-ONLY" in text and "25.5000" in text
    def boom(*a):
        raise OSError("network down")
    text2, recs = dg.run("TESTETF", "2026-09-14", "2026-09-18", root, registry_file(tmp_path), compare_yahoo=True, yahoo_fn=boom)
    assert "YAHOO COMPARISON UNAVAILABLE: OSError: network down" in text2 and recs


def test_yahoo_parser_maps_timestamps_to_ist_dates():
    import io
    payload = {"chart": {"result": [{"timestamp": [1789000000, 1789086400], "indicators": {"quote": [{"close": [10.5, None]}]}}]}}

    class R(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    out = dg.yahoo_closes("X", "2026-09-10", "2026-09-12", opener=lambda req, timeout=0: R(json.dumps(payload).encode()))
    assert list(out.values()) == [10.5] and len(list(out)[0]) == 10


def test_nearest_round_ratio_handles_reverse_splits():
    assert dg.nearest_round_ratio(10.0) == (10.0, 0.0)
    c, e = dg.nearest_round_ratio(0.1)
    assert c == pytest.approx(0.1) and e == pytest.approx(0.0)
    c, e = dg.nearest_round_ratio(4.9)
    assert c == 5.0 and e == pytest.approx(0.02)


def test_missing_file_returns_1(tmp_path, capsys):
    rc = dg.main(["NOSUCH", "--eod2-root", str(tmp_path)])
    assert rc == 1 and "EOD2 file not found" in capsys.readouterr().err


def test_cli_out_file_and_no_move_case(tmp_path, capsys):
    root = make_eod2(tmp_path, ROWS[:3])
    out = tmp_path / "rep" / "r.txt"
    rc = dg.main(["TESTETF", "--eod2-root", root, "--registry", registry_file(tmp_path), "--manifest", "", "--out", str(out)])
    assert rc == 0 and "No |return| >= 15% in the range." in out.read_text(encoding="utf-8")


def test_works_on_the_shared_eod2_min_fixture():
    root = r"C:\dev\fixtures\eod2_min"
    if not os.path.isdir(root):
        pytest.skip("fixtures missing")
    text, recs = dg.run("NIFTYBEES", "0000-00-00", "9999-99-99", root, None)
    assert len(recs) == 30 and "DIAGNOSIS NIFTYBEES" in text
