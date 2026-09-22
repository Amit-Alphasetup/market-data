"""D0: registry validator — a valid document passes and each rule has a failing case."""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dataops import schema_universe as su  # noqa: E402


def base():
    return {
        "version": 3, "defaultHistoryStart": "2019-01-14",
        "calendar": {"cutoffIST": "19:15", "holidaySource": "eod2_meta"},
        "instruments": [
            {"id": "NSE:NIFTYBEES", "symbol": "NIFTYBEES", "kind": "ETF", "exchange": "NSE", "isin": "INF204KB14I2", "name": "Nifty BeES",
             "assetClass": "equity", "engines": ["LIFO", "DM", "TENET"], "linkedIndex": "NIFTY 50", "listingDate": None,
             "listingDateEvidence": None, "delistingDate": None, "aliases": [], "eod2File": None, "historyStart": None, "enabled": True},
            {"id": "NSE:GOLDBEES", "symbol": "GOLDBEES", "kind": "ETF", "exchange": "NSE", "isin": "", "name": "Gold BeES",
             "assetClass": "gold", "engines": [], "linkedIndex": None, "listingDate": None,
             "listingDateEvidence": None, "delistingDate": None, "aliases": [], "eod2File": None, "historyStart": None, "enabled": True},
        ],
        "indices": [{"id": "IDX:NIFTY 50", "name": "NIFTY 50", "kind": "INDEX", "eod2File": None, "requiredFields": ["open", "high", "close"],
                     "historyStart": None, "enabled": True}],
        "splits": [{"symbol": "GOLDBEES", "date": "2019-12-19", "ratioNum": 100, "ratioDen": 1, "evidence": None, "approvedAt": None,
                    "provenance": "migrated-v2-2026-09-11"}],
        "genuineMoves": [{"symbol": "NIFTYBEES", "date": "2020-03-12", "evidence": "NSE circular 123", "approvedAt": "2026-09-19T10:00:00+05:30",
                          "provenance": "dataops"}],
        "quarantine": {}, "gapPolicy": {},
    }


def test_valid_document_passes():
    assert su.validate(base()) == []


def mut(fn):
    r = base()
    fn(r)
    return r


CASES = {
    "missing top key": (lambda r: r.pop("indices"), "missing key 'indices'"),
    "unexpected top key": (lambda r: r.update(bogus=1), "unexpected key 'bogus'"),
    "wrong version": (lambda r: r.update(version=2), "$.version"),
    "bad defaultHistoryStart": (lambda r: r.update(defaultHistoryStart="2019-02-30"), "defaultHistoryStart"),
    "bad cutoff": (lambda r: r["calendar"].update(cutoffIST="25:00"), "cutoffIST"),
    "bad holidaySource": (lambda r: r["calendar"].update(holidaySource="x"), "holidaySource"),
    "bad symbol": (lambda r: r["instruments"][0].update(symbol="nifty bees"), "symbol"),
    "duplicate symbol": (lambda r: r["instruments"][1].update(symbol="NIFTYBEES", id="NSE:NIFTYBEES"), "duplicate"),
    "id mismatch": (lambda r: r["instruments"][0].update(id="NSE:OTHER"), ".id"),
    "bad kind": (lambda r: r["instruments"][0].update(kind="FUND"), ".kind"),
    "bad exchange": (lambda r: r["instruments"][0].update(exchange="BSE"), "exchange"),
    "bad isin": (lambda r: r["instruments"][0].update(isin="123"), "isin"),
    "bad assetClass": (lambda r: r["instruments"][0].update(assetClass="crypto"), "assetClass"),
    "bad engine": (lambda r: r["instruments"][0].update(engines=["LIFO", "XYZ"]), "engines"),
    "duplicate engine": (lambda r: r["instruments"][0].update(engines=["LIFO", "LIFO"]), "engines"),
    "listingDate without evidence": (lambda r: r["instruments"][0].update(listingDate="2005-01-01"), "evidence"),
    "evidence without listingDate": (lambda r: r["instruments"][0].update(listingDateEvidence="x"), "listingDateEvidence"),
    "bad listingDate": (lambda r: r["instruments"][0].update(listingDate="2005-13-01", listingDateEvidence="x"), "listingDate"),
    "bad alias": (lambda r: r["instruments"][0].update(aliases=[{"symbol": "OLD", "from": "2020-01-01"}]), "aliases"),
    "bad historyStart": (lambda r: r["instruments"][0].update(historyStart="yesterday"), "historyStart"),
    "enabled not bool": (lambda r: r["instruments"][0].update(enabled="yes"), "enabled"),
    "index bad id": (lambda r: r["indices"][0].update(id="IDX:X"), ".id"),
    "index bad field": (lambda r: r["indices"][0].update(requiredFields=["open", "vwap"]), "requiredFields"),
    "index empty fields": (lambda r: r["indices"][0].update(requiredFields=[]), "requiredFields"),
    "index duplicate": (lambda r: r["indices"].append(copy.deepcopy(r["indices"][0])), "duplicate"),
    "split unknown symbol": (lambda r: r["splits"][0].update(symbol="NOPE"), "symbol"),
    "split bad date": (lambda r: r["splits"][0].update(date="2019-12-32"), "date"),
    "split ratio zero": (lambda r: r["splits"][0].update(ratioNum=0), "ratioNum"),
    "split ratio float": (lambda r: r["splits"][0].update(ratioDen=1.5), "ratioDen"),
    "migrated with fabricated evidence": (lambda r: r["splits"][0].update(evidence="made up"), "never fabricated"),
    "migrated with approvedAt": (lambda r: r["splits"][0].update(approvedAt="2026-09-19T00:00:00"), "never fabricated"),
    "dataops without evidence": (lambda r: r["genuineMoves"][0].update(evidence=None), "requires non-empty evidence"),
    "dataops without approvedAt": (lambda r: r["genuineMoves"][0].update(approvedAt=None), "requires approvedAt"),
    "evidence blank": (lambda r: r["genuineMoves"][0].update(evidence="  "), "evidence"),
    "approvedAt malformed": (lambda r: r["genuineMoves"][0].update(approvedAt="soon"), "approvedAt"),
    "provenance missing": (lambda r: r["genuineMoves"][0].update(provenance=""), "provenance"),
    "duplicate approval for one date": (lambda r: r["genuineMoves"].append(copy.deepcopy(r["genuineMoves"][0])), "per date"),
    "move extra key": (lambda r: r["genuineMoves"][0].update(ratioNum=2), "unexpected key"),
    "quarantine wrong type": (lambda r: r.update(quarantine="x"), "quarantine"),
    "gapPolicy wrong type": (lambda r: r.update(gapPolicy=[]), "gapPolicy"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_rule_has_failing_case(name):
    fn, needle = CASES[name]
    errs = su.validate(mut(fn))
    assert errs, f"{name}: expected a validation error"
    assert any(needle in e for e in errs), f"{name}: {errs} should mention {needle!r}"


def test_two_approvals_on_different_dates_are_fine():
    r = base()
    r["genuineMoves"].append({**r["genuineMoves"][0], "date": "2020-03-13"})
    assert su.validate(r) == []


def test_bad_prints_is_optional_but_validated_like_genuine_moves_when_present():
    r = base()
    assert "badPrints" not in r and su.validate(r) == []            # absent entirely: still a valid document
    r["badPrints"] = [{"symbol": "NIFTYBEES", "date": "2020-04-01", "evidence": "vendor glitch, confirmed against NSE bhavcopy",
                        "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"}]
    assert su.validate(r) == []
    r["badPrints"][0]["evidence"] = None
    assert any("requires non-empty evidence" in e for e in su.validate(r))
    r2 = base()
    r2["badPrints"] = [{"symbol": "NIFTYBEES", "date": "2020-03-12", "evidence": "e", "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"},
                        {"symbol": "NIFTYBEES", "date": "2020-03-12", "evidence": "e2", "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"}]
    assert any("per date" in e for e in su.validate(r2))            # duplicate flag for the same date is rejected, same as genuineMoves


def test_is_date_rejects_impossible_dates():
    assert su.is_date("2024-02-29") and not su.is_date("2023-02-29") and not su.is_date("2024-2-9") and not su.is_date(None)


def test_assert_valid_raises_with_all_errors():
    r = mut(lambda x: (x["instruments"][0].update(assetClass="crypto"), x.update(version=9)))
    with pytest.raises(ValueError) as e:
        su.assert_valid(r)
    assert "assetClass" in str(e.value) and "version" in str(e.value)
    json.dumps(base())
