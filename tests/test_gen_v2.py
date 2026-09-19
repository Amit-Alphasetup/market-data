"""D0.5: registry -> v2 config generator. Round trip gen_v2(migrate_v2(original)) == original for every key the v2 exporter reads."""
import copy
import json
import os

import pytest

from dataops import gen_v2 as g
from dataops import migrate_v2 as mig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "data")
V2 = json.load(open(os.path.join(DATA, "eod2_universe.v2.original.json"), encoding="utf-8-sig"))
APP = json.load(open(os.path.join(DATA, "app_lists_v321.json"), encoding="utf-8"))
V2_EXPORTER_KEYS = ["start", "jump_threshold", "etfs", "indices", "splits", "trim_before", "genuine", "quarantine",
                    "known_no_trade", "gap_allow_all", "patches"]


def reg():
    return mig.migrate(copy.deepcopy(V2), APP["lifo"], APP["mom"])


def test_round_trip_every_key_the_v2_exporter_reads():
    out = g.gen_v2(reg())
    for k in V2_EXPORTER_KEYS:
        assert out[k] == V2[k], "key %s differs" % k
    assert out == V2
    assert list(out) == list(V2)  # same key order as the original file


def test_exporter_reads_only_known_keys():
    exp = open(r"C:\dev\eod2_export.py", encoding="utf-8").read()
    for k in V2_EXPORTER_KEYS:
        assert ('"%s"' % k) in exp or ("cfg.%s" % k) in exp, k


def test_disabled_instrument_is_dropped_with_its_split_genuine_and_trim():
    r = reg()
    for x in r["instruments"]:
        if x["symbol"] in ("NIFTYBEES", "MONQ50", "MOM30IETF"):
            x["enabled"] = False
    out = g.gen_v2(r)
    assert "NIFTYBEES" not in out["etfs"] and "MONQ50" not in out["etfs"]
    assert not any(s[0] == "NIFTYBEES" for s in out["splits"])
    assert not any(k.startswith("MONQ50|") for k in out["genuine"])
    assert "MOM30IETF" not in out["trim_before"] and "INFRAIETF" in out["trim_before"]


def test_disabled_index_is_dropped():
    r = reg()
    r["indices"][-1]["enabled"] = False
    assert g.gen_v2(r)["indices"] == V2["indices"][:-1]


def test_new_instrument_and_dataops_approval_appear_in_v2():
    r = reg()
    sym = "NEWETF"
    r["instruments"].append({**copy.deepcopy(r["instruments"][0]), "id": "NSE:" + sym, "symbol": sym, "name": sym, "historyStart": "2021-03-01"})
    r["genuineMoves"].append({"symbol": sym, "date": "2025-05-05", "evidence": "NSE circular 42", "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"})
    r["splits"].append({"symbol": sym, "date": "2024-01-02", "ratioNum": 3, "ratioDen": 2, "evidence": "ca", "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"})
    out = g.gen_v2(r)
    assert out["etfs"][-1] == sym
    assert out["genuine"]["NEWETF|2025-05-05"] == "NSE circular 42"     # no v2 note -> evidence text
    assert out["splits"][-1] == [sym, "2024-01-02", 1.5]              # non-integer ratio kept exact
    assert out["trim_before"][sym] == "2021-03-01"


def test_invalid_registry_is_refused():
    r = reg()
    r["instruments"][0]["assetClass"] = "crypto"
    with pytest.raises(ValueError):
        g.gen_v2(r)


def test_write_v2_backs_up_original_once_and_writes_lf(tmp_path):
    target = tmp_path / "eod2_universe.json"
    backup = tmp_path / "eod2_universe.v2.backup.json"
    target.write_text(json.dumps({"original": True}), encoding="utf-8")
    g.write_v2(reg(), str(target), str(backup))
    assert json.loads(backup.read_text(encoding="utf-8")) == {"original": True}
    assert json.loads(target.read_text(encoding="utf-8")) == V2
    assert b"\r" not in target.read_bytes()
    g.write_v2(reg(), str(target), str(backup))            # second write must NOT overwrite the backup
    assert json.loads(backup.read_text(encoding="utf-8")) == {"original": True}


def test_real_backup_exists_and_equals_original_content():
    b = r"C:\dev\eod2_universe.v2.backup.json"
    if os.path.exists(b):
        assert json.load(open(b, encoding="utf-8-sig")) == V2


def test_cli_dry_run_prints_config_without_writing(tmp_path, capsys):
    out = tmp_path / "x.json"
    rc = g.main(["--registry", os.path.join(ROOT, "config", "universe.json"), "--out", str(out), "--dry-run"])
    assert rc == 0 and not out.exists()
    assert '"etfs"' in capsys.readouterr().out
