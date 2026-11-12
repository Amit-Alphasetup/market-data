"""Add NSE equities to config/universe.json from an index-constituents CSV (niftyindices.com format:
Company Name, Industry, Symbol, Series, ISIN Code).  Usage:
    python scripts/add_stocks.py ind_nifty500list.csv [--config config/universe.json] [--dry-run]
Idempotent: existing symbols are left untouched. New rows are kind=EQ, enabled=true, engines=[]. Validates before writing."""
import argparse, csv, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from dataops.schema_universe import validate, SYMBOL_RE, ISIN_RE


def rows_from_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
            sym = r.get("Symbol", "").upper()
            if r.get("Series", "EQ") not in ("EQ", "BE", "BZ", ""):
                continue
            yield sym, r.get("Company Name", "") or sym, r.get("ISIN Code", "")


def add_stocks(reg, rows):
    have = {i["symbol"] for i in reg["instruments"]}
    added, skipped = [], []
    for sym, name, isin in rows:
        if not SYMBOL_RE.match(sym) or sym in have:
            skipped.append(sym); continue
        reg["instruments"].append({
            "id": "NSE:" + sym, "symbol": sym, "kind": "EQ", "exchange": "NSE",
            "isin": isin if ISIN_RE.match(isin) else "", "name": name, "assetClass": "equity",
            "engines": [], "linkedIndex": None, "listingDate": None, "listingDateEvidence": None,
            "delistingDate": None, "aliases": [], "eod2File": None, "historyStart": None, "enabled": True})
        have.add(sym); added.append(sym)
    return added, skipped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("csv"); ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config", "universe.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf-8") as fh:
        reg = json.load(fh)
    added, skipped = add_stocks(reg, rows_from_csv(a.csv))
    errs = validate(reg)
    if errs:
        print("INVALID, nothing written:\n" + "\n".join(errs[:20])); return 1
    print("added %d, skipped %d" % (len(added), len(skipped)))
    if not a.dry_run:
        with open(a.config, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=2, ensure_ascii=False); fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
