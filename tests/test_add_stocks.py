import copy, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import add_stocks
from dataops.schema_universe import validate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reg():
    with open(os.path.join(REPO, "config", "universe.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_adds_valid_eq_rows_and_is_idempotent(tmp_path):
    p = tmp_path / "n.csv"
    p.write_text("Company Name,Industry,Symbol,Series,ISIN Code\nReliance Industries Ltd.,Oil,RELIANCE,EQ,INE002A01018\nM&M Ltd.,Auto,M&M,EQ,INE101A01026\nBad,x,lower case!,EQ,\nNiftyBees dup,x,NIFTYBEES,EQ,\n", encoding="utf-8")
    reg = _reg(); n0 = len(reg["instruments"])
    added, skipped = add_stocks.add_stocks(reg, add_stocks.rows_from_csv(str(p)))
    assert added == ["RELIANCE", "M&M"] and len(skipped) == 2
    assert len(reg["instruments"]) == n0 + 2 and validate(reg) == []
    again, _ = add_stocks.add_stocks(reg, add_stocks.rows_from_csv(str(p)))
    assert again == []
