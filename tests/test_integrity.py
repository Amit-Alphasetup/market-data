"""EOD2 daily-CSV integrity scan (2026-10-18). The Sep-2026 outage was EOD2 appending rows without a trailing newline (two dates
fused on one line) and re-appending on reruns (duplicate dates); pandas then crashed on a corporate-action adjustment and every
nightly update failed for three weeks. The scan is read-only; sync refuses to start EOD2 on corrupt data and names the files."""
import os

import pytest

from dataops import cli
from dataops import integrity as ig
from dataops import sync as sy
from test_d2_ops_sync import NOW, fake_layout

HDR = "Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"
ROW = "2026-09-%02d,10.0,11.0,9.0,10.5,1000,EQ,,,\n"


def daily(tmp_path, files):
    d = tmp_path / "data" / "daily"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_bytes(text.encode("utf-8"))
    return str(tmp_path / "data")


def test_clean_files_pass(tmp_path):
    root = daily(tmp_path, {"aaa.csv": HDR + ROW % 1 + ROW % 2, "hdr_only.csv": HDR})
    rep = ig.scan_daily(root)
    assert rep["files"] == 2 and rep["bad"] == []


def test_each_corruption_kind_is_named_with_its_line(tmp_path):
    root = daily(tmp_path, {
        "fused.csv": HDR + ROW % 1 + (ROW % 2).rstrip("\n") + ROW % 3,     # 2026-09-02 and -03 on one line (no newline)
        "dupe.csv": HDR + ROW % 1 + ROW % 2 + ROW % 2,
        "empty.csv": "",
        "ragged.csv": HDR + ROW % 1 + "2026-09-02,10.0,11.0\n",
        "ok.csv": HDR + ROW % 1,
    })
    bad = {b["file"]: b for b in ig.scan_daily(root)["bad"]}
    assert set(bad) == {"fused.csv", "dupe.csv", "empty.csv", "ragged.csv"}
    assert bad["fused.csv"]["problem"] == "FUSED_ROWS" and bad["fused.csv"]["line"] == 3
    assert bad["dupe.csv"]["problem"] == "DUPLICATE_DATE" and "2026-09-02" in bad["dupe.csv"]["detail"]
    assert bad["empty.csv"]["problem"] == "EMPTY"
    assert bad["ragged.csv"]["problem"] == "RAGGED_ROW" and bad["ragged.csv"]["line"] == 3


def test_summary_text_names_files_and_the_repair_script(tmp_path):
    root = daily(tmp_path, {"a%d.csv" % i: "" for i in range(12)})
    txt = ig.summary(ig.scan_daily(root))
    assert "12 corrupt EOD2 daily CSV" in txt and "a0.csv" in txt and "+2 more" in txt and "repair_eod2_csvs.py" in txt


def test_sync_refuses_to_start_eod2_on_corrupt_data_and_names_the_file(tmp_path):
    lay, patch = fake_layout(tmp_path)
    with open(os.path.join(lay["dataRoot"], "daily", "aastha.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(HDR + (ROW % 1).rstrip("\n") + ROW % 2)
    with pytest.raises(sy.SyncFail, match="aastha.csv"):
        sy.sync(lay, now=NOW, runner=lambda *a: pytest.fail("EOD2 must not start on corrupt data"), running_check=lambda: [], log_dir=str(tmp_path / "logs"), patch_file=patch)


def test_cli_check_eod2_exit_codes(tmp_path, capsys):
    good = daily(tmp_path / "g", {"a.csv": HDR + ROW % 1})
    bad = daily(tmp_path / "b", {"a.csv": ""})
    assert cli.main(["check-eod2", "--data-root", good]) == 0
    assert cli.main(["check-eod2", "--data-root", bad]) == 1
    assert "a.csv" in capsys.readouterr().out
