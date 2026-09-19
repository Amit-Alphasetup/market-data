"""D0 tests: v2 -> v3 registry migration (plan D0 step 9). Hermetic: uses tests/data snapshots of the v2 config and AlphaDesk lists."""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dataops import migrate_v2 as mig  # noqa: E402
from dataops import schema_universe as su  # noqa: E402

DATA = os.path.join(ROOT, "tests", "data")


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8-sig") as f:
        return json.load(f)


V2 = load("eod2_universe.v2.original.json")
APP = load("app_lists_v321.json")
CERT = {n: ["open", "high", "close"] for n in V2["indices"]}


def do_migrate(**kw):
    return mig.migrate(copy.deepcopy(V2), APP["lifo"], APP["mom"], cert_fields=CERT, **kw)


def test_counts_equal():
    reg = do_migrate()
    assert len(reg["instruments"]) == len(V2["etfs"]) == 40
    assert len(reg["indices"]) == len(V2["indices"]) == 6
    assert len(reg["splits"]) == len(V2["splits"]) == 12
    assert len(reg["genuineMoves"]) == len(V2["genuine"]) == 7
    assert [x["symbol"] for x in reg["instruments"]] == V2["etfs"]  # order preserved
    assert [x["name"] for x in reg["indices"]] == V2["indices"]


def test_splits_preserved_with_real_dates_and_provenance():
    reg = do_migrate()
    for (sym, d, ratio), e in zip(V2["splits"], reg["splits"]):
        assert (e["symbol"], e["date"], e["ratioNum"], e["ratioDen"]) == (sym, d, ratio, 1)
        assert e["provenance"] == "migrated-v2-2026-09-11"


def test_genuine_moves_preserved_with_real_dates_and_provenance():
    reg = do_migrate()
    got = {(e["symbol"], e["date"]): e for e in reg["genuineMoves"]}
    for key, note in V2["genuine"].items():
        sym, d = key.split("|")
        e = got[(sym, d)]
        assert e["provenance"] == "migrated-v2-2026-09-11"
        assert e["note"] == note  # v2 free text kept verbatim (lossless), NOT promoted to evidence


def test_nothing_fabricated():
    reg = do_migrate()
    for e in reg["splits"] + reg["genuineMoves"]:
        assert e["evidence"] is None and e["approvedAt"] is None
    for x in reg["instruments"]:
        assert x["listingDate"] is None and x["listingDateEvidence"] is None and x["delistingDate"] is None
        assert x["isin"] == "" and x["linkedIndex"] is None and x["aliases"] == [] and x["eod2File"] is None
    text = json.dumps(reg)
    assert "PLACEHOLDER" not in text.upper() and "TODO" not in text.upper()


def test_every_instrument_valid():
    reg = do_migrate()
    assert su.validate(reg) == []
    for x in reg["instruments"]:
        one = copy.deepcopy(reg)
        one["instruments"] = [x]
        one["splits"] = [s for s in reg["splits"] if s["symbol"] == x["symbol"]]
        one["genuineMoves"] = [g for g in reg["genuineMoves"] if g["symbol"] == x["symbol"]]
        assert su.validate(one) == [], x["symbol"]


def test_engines_from_app_tags():
    reg = {x["symbol"]: x for x in do_migrate()["instruments"]}
    assert reg["NIFTYBEES"]["engines"] == ["LIFO", "DM"]     # 'both'
    assert reg["TATSILV"]["engines"] == ["DM"]              # momentum only
    assert reg["LIQUIDBEES"]["engines"] == ["LIFO"]         # lifo only
    assert reg["MIDCAPETF"]["engines"] == []                # unknown -> [] and listed
    assert all("TENET" not in x["engines"] for x in reg.values())  # Tenet pairs are runtime state: never invented
    reg2 = {x["symbol"]: x for x in do_migrate(tenet_symbols=["NIFTYBEES", "BANKBEES"])["instruments"]}
    assert reg2["NIFTYBEES"]["engines"] == ["LIFO", "DM", "TENET"] and reg2["BANKBEES"]["engines"][-1] == "TENET"


def test_asset_class_table():
    reg = {x["symbol"]: x["assetClass"] for x in do_migrate()["instruments"]}
    assert reg["GOLDBEES"] == "gold" and reg["SETFGOLD"] == "gold"
    assert reg["SILVERBEES"] == "silver" and reg["TATSILV"] == "silver"
    assert reg["MON100"] == "international" and reg["MONQ50"] == "international" and reg["HNGSNGBEES"] == "international" and reg["MAFANG"] == "international"
    assert reg["LIQUIDBEES"] == "liquid"
    assert reg["NIFTYBEES"] == "equity" and reg["ITBEES"] == "equity"


def test_trim_before_becomes_history_start_and_policies_verbatim():
    reg = do_migrate()
    hs = {x["symbol"]: x["historyStart"] for x in reg["instruments"] if x["historyStart"]}
    assert hs == V2["trim_before"]
    assert reg["quarantine"] == V2["quarantine"]
    assert reg["gapPolicy"] == {"known_no_trade": V2["known_no_trade"], "gap_allow_all": V2["gap_allow_all"]}
    assert reg["legacyV2"] == {"jump_threshold": V2["jump_threshold"], "patches": V2["patches"]}
    assert reg["defaultHistoryStart"] == V2["start"]


def test_index_required_fields_from_cert_and_close_only_history_start():
    reg = do_migrate()
    for i in reg["indices"]:
        assert i["requiredFields"] == ["open", "high", "close"] and i["historyStart"] is None
    # an index whose older history is close-only gets historyStart = first date with all required fields
    stub = lambda name, fields, from_date: "2019-06-03" if name == "NIFTY MIDCAP 150" else from_date  # noqa: E731
    reg2 = mig.migrate(copy.deepcopy(V2), APP["lifo"], APP["mom"], cert_fields=CERT, index_first_full=stub)
    by = {i["name"]: i["historyStart"] for i in reg2["indices"]}
    assert by["NIFTY MIDCAP 150"] == "2019-06-03" and by["NIFTY 50"] is None
    # cert missing -> O/H/C default
    reg3 = mig.migrate(copy.deepcopy(V2), APP["lifo"], APP["mom"], cert_fields={})
    assert all(i["requiredFields"] == ["open", "high", "close"] for i in reg3["indices"])
    # cert says low is required for one index -> respected
    reg4 = mig.migrate(copy.deepcopy(V2), APP["lifo"], APP["mom"], cert_fields={"NIFTY 50": ["open", "high", "low", "close"]})
    assert reg4["indices"][0]["requiredFields"] == ["open", "high", "low", "close"]


def test_first_full_field_date_reads_csv(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "nifty x.csv").write_text("Date,Open,High,Low,Close,Volume\n2019-01-14,,,,100,1\n2019-01-15,,,,101,1\n2019-01-16,100,102,99,101,1\n", encoding="utf-8")
    assert mig.first_full_field_date(str(tmp_path), "NIFTY X", ["open", "high", "close"], "2019-01-14") == "2019-01-16"
    assert mig.first_full_field_date(str(tmp_path), "NIFTY X", ["close"], "2019-01-14") == "2019-01-14"
    assert mig.first_full_field_date(str(tmp_path), "NOPE", ["close"], "2019-01-14") is None


def test_migration_is_deterministic_and_dumps_lf_utf8():
    a = mig.dumps(do_migrate())
    b = mig.dumps(do_migrate())
    assert a == b and "\r" not in a and a.endswith("\n")


def test_committed_registry_is_valid():
    with open(os.path.join(ROOT, "config", "universe.json"), encoding="utf-8") as f:
        reg = json.load(f)
    assert su.validate(reg) == []
    assert reg["version"] == 3


def test_parse_app_lists_from_real_index_html_matches_snapshot():
    path = r"C:\dev\alphadesk\index.html"
    if not os.path.isfile(path):
        return
    lifo, mom = mig.parse_app_lists(path)
    assert lifo["NIFTYBEES"] == "Nifty 50 Large Cap" and "NIFTYBEES" in mom
    assert len(lifo) >= 30 and len(mom) >= 30
