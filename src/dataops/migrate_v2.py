"""Migrate the v2 exporter config (C:\\dev\\eod2_universe.json) into the v3 registry (config/universe.json) — plan D0 step 5.

Usage:
  py -m dataops.migrate_v2 [--v2 C:\\dev\\eod2_universe.json] [--app C:\\dev\\alphadesk\\index.html]
                           [--cert-csv C:\\dev\\eod2_cert_report\\eod2_certification.csv]
                           [--eod2-data-root C:\\dev\\eod2\\src\\eod2_data] [--tenet-symbols FILE] [--out config\\universe.json]

Rules (never invented):
  * engines: LIFO if the symbol is in AlphaDesk DEFAULT_LIFO_UNIVERSE, DM if in DEFAULT_MOM_UNIVERSE, TENET only if listed in
    --tenet-symbols (live Tenet pairs are runtime state, not in index.html). None matched -> [] and listed for Apd (Q2).
  * listingDate / evidence: always null. isin: always ''. linkedIndex: null (unknown from files).
  * splits / genuineMoves keep their real dates; evidence=null, approvedAt=null, provenance='migrated-v2-2026-09-11'.
    The v2 free-text note of a genuine move is kept verbatim in the optional `note` field (lossless; needed by gen_v2).
  * quarantine / gapPolicy copied verbatim from v2 (gapPolicy = {known_no_trade, gap_allow_all}); trim_before -> instrument historyStart.
  * v2 keys with no home in the v3 schema (jump_threshold, patches) are kept losslessly under top-level `legacyV2`.
"""
import argparse
import csv
import json
import os
import re
import sys

from . import schema_universe as su

PROVENANCE = su.MIGRATED_PROVENANCE

GOLD = {"GOLDBEES", "SETFGOLD", "HDFCGOLD", "TATAGOLD", "GOLDIETF", "GOLD1"}
SILVER = {"SILVERBEES", "TATSILV", "HDFCSILVER", "SILVERIETF", "SBISILVER", "SILVER"}
INTERNATIONAL = {"MAFANG", "MON100", "MONQ50", "HNGSNGBEES"}
LIQUID = {"LIQUIDBEES"}


def asset_class(sym):
    if sym in GOLD:
        return "gold"
    if sym in SILVER:
        return "silver"
    if sym in INTERNATIONAL:
        return "international"
    if sym in LIQUID:
        return "liquid"
    return "equity"


def parse_app_lists(index_html_path):
    """Read DEFAULT_LIFO_UNIVERSE {sym: name} and DEFAULT_MOM_UNIVERSE [sym] from AlphaDesk index.html (read-only)."""
    with open(index_html_path, encoding="utf-8") as f:
        txt = f.read()
    m = re.search(r"const DEFAULT_LIFO_UNIVERSE\s*=\s*\[(.*?)\n\];", txt, re.S)
    if not m:
        raise ValueError("DEFAULT_LIFO_UNIVERSE not found in " + index_html_path)
    lifo = {}
    for e in re.finditer(r"\{sym:'([^']+)'[^}]*?name:'([^']*)'", m.group(1)):
        lifo[e.group(1)] = e.group(2)
    m2 = re.search(r"const DEFAULT_MOM_UNIVERSE\s*=\s*\[(.*?)\];", txt, re.S)
    if not m2:
        raise ValueError("DEFAULT_MOM_UNIVERSE not found in " + index_html_path)
    mom = re.findall(r"'([^']+)'", m2.group(1))
    return lifo, mom


def read_cert_index_fields(cert_csv):
    """{index name: [lowercase required fields]} from the certification CSV (rows with kind INDEX)."""
    out = {}
    if not cert_csv or not os.path.isfile(cert_csv):
        return out
    with open(cert_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("kind", "")).upper() == "INDEX" and r.get("required_fields"):
                out[r["name"]] = [x.strip().lower() for x in r["required_fields"].split(",") if x.strip()]
    return out


def first_full_field_date(eod2_root, index_name, fields, from_date):
    """First date >= from_date where every required field is present (reads the EOD2 CSV; read-only)."""
    path = os.path.join(eod2_root, "daily", index_name.lower() + ".csv")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (rd.fieldnames or [])}
        rows = []
        for r in rd:
            d = (r.get(cols.get("date", "")) or "")[:10]
            if len(d) == 10:
                rows.append((d, r))
    rows.sort(key=lambda t: t[0])

    def num(v):
        try:
            x = float(v)
            return x == x and x > 0
        except (TypeError, ValueError):
            return False

    for d, r in rows:
        if d >= from_date and all(f in cols and num(r.get(cols[f])) for f in fields):
            return d
    return None


def migrate(v2, lifo_names, mom_list, tenet_symbols=(), cert_fields=None, index_first_full=None):
    """Pure conversion. index_first_full: optional callable(name, fields, from_date) -> first full-field date or None."""
    cert_fields = cert_fields or {}
    tenet = set(tenet_symbols)
    mom = set(mom_list)
    trim = v2.get("trim_before", {}) or {}
    default_start = v2["start"]
    instruments = []
    for sym in v2["etfs"]:
        engines = []
        if sym in lifo_names:
            engines.append("LIFO")
        if sym in mom:
            engines.append("DM")
        if sym in tenet:
            engines.append("TENET")
        instruments.append({
            "id": "NSE:" + sym, "symbol": sym, "kind": "ETF", "exchange": "NSE", "isin": "",
            "name": lifo_names.get(sym, sym), "assetClass": asset_class(sym), "engines": engines,
            "linkedIndex": None, "listingDate": None, "listingDateEvidence": None, "delistingDate": None,
            "aliases": [], "eod2File": None, "historyStart": trim.get(sym), "enabled": True,
        })
    indices = []
    for name in v2["indices"]:
        fields = cert_fields.get(name) or ["open", "high", "close"]
        hs = None
        if index_first_full:
            first = index_first_full(name, fields, default_start)
            if first and first > default_start:
                hs = first
        indices.append({"id": "IDX:" + name, "name": name, "kind": "INDEX", "eod2File": None,
                        "requiredFields": fields, "historyStart": hs, "enabled": True})
    splits = [{"symbol": s, "date": d, "ratioNum": int(r), "ratioDen": 1, "evidence": None, "approvedAt": None, "provenance": PROVENANCE}
              for s, d, r in v2.get("splits", [])]
    moves = []
    for key, note in (v2.get("genuine", {}) or {}).items():
        sym, d = key.split("|")
        moves.append({"symbol": sym, "date": d, "evidence": None, "approvedAt": None, "provenance": PROVENANCE, "note": note})
    reg = {
        "version": 3,
        "defaultHistoryStart": default_start,
        "calendar": {"cutoffIST": "19:15", "holidaySource": "eod2_meta"},
        "instruments": instruments,
        "indices": indices,
        "splits": splits,
        "genuineMoves": moves,
        "quarantine": json.loads(json.dumps(v2.get("quarantine", {}))),
        "gapPolicy": {"known_no_trade": json.loads(json.dumps(v2.get("known_no_trade", {}))),
                      "gap_allow_all": json.loads(json.dumps(v2.get("gap_allow_all", {})))},
        "legacyV2": {"jump_threshold": v2.get("jump_threshold", 0.15), "patches": json.loads(json.dumps(v2.get("patches", {})))},
    }
    return reg


def report(reg):
    """Human-readable confirmation tables for Apd (assetClass table, empty ISINs, engines TODO)."""
    lines = ["ASSET CLASS TABLE (confirm):"]
    for x in reg["instruments"]:
        lines.append(f"  {x['symbol']:<12} {x['assetClass']:<14} engines={','.join(x['engines']) or '-'}")
    empty_isin = [x["symbol"] for x in reg["instruments"] if not x["isin"]]
    lines.append(f"EMPTY ISIN ({len(empty_isin)}): " + ", ".join(empty_isin))
    todo = [x["symbol"] for x in reg["instruments"] if not x["engines"]]
    lines.append(f"ENGINES TODO — not in AlphaDesk LIFO/DM lists ({len(todo)}): " + (", ".join(todo) or "none"))
    no_tenet = [x["symbol"] for x in reg["instruments"] if "TENET" not in x["engines"]]
    lines.append(f"TENET not assigned to any ETF ({len(no_tenet)} of {len(reg['instruments'])}): live Tenet pairs are runtime state; "
                 "assign with `dataops set-engines` or --tenet-symbols.")
    lines.append("INDICES: " + "; ".join(f"{i['name']} req={','.join(i['requiredFields'])} historyStart={i['historyStart']}" for i in reg["indices"]))
    return "\n".join(lines)


def dumps(reg):
    return json.dumps(reg, indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--v2", default=r"C:\dev\eod2_universe.json")
    ap.add_argument("--app", default=r"C:\dev\alphadesk\index.html")
    ap.add_argument("--cert-csv", default=r"C:\dev\eod2_cert_report\eod2_certification.csv")
    ap.add_argument("--eod2-data-root", default=r"C:\dev\eod2\src\eod2_data")
    ap.add_argument("--tenet-symbols", default=None, help="text file, one symbol per line, ETFs used in any Tenet pair")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config", "universe.json"))
    a = ap.parse_args(argv)
    with open(a.v2, encoding="utf-8-sig") as f:
        v2 = json.load(f)
    lifo, mom = parse_app_lists(a.app)
    tenet = []
    if a.tenet_symbols:
        with open(a.tenet_symbols, encoding="utf-8") as f:
            tenet = [ln.strip().upper() for ln in f if ln.strip()]
    cert = read_cert_index_fields(a.cert_csv)
    ff = (lambda n, fl, fd: first_full_field_date(a.eod2_data_root, n, fl, fd)) if os.path.isdir(a.eod2_data_root) else None
    reg = migrate(v2, lifo, mom, tenet, cert, ff)
    su.assert_valid(reg)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(reg))
    print(report(reg))
    print(f"\nwrote {a.out}: {len(reg['instruments'])} ETFs, {len(reg['indices'])} indices, {len(reg['splits'])} splits, {len(reg['genuineMoves'])} genuine moves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
