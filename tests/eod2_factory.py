"""Builds synthetic EOD2 data folders + registries for exporter tests (mimics the real layout: daily/<lowercase>.csv, meta.json, ...)."""
import copy
import json
import os
from datetime import date, timedelta

HDR_ETF = "Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY"
HDR_IDX = "Date,Open,High,Low,Close,Volume,P/E,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY"


def weekdays(start, n, skip=()):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        iso = d.isoformat()
        if d.weekday() < 5 and iso not in skip:
            out.append(iso)
        d += timedelta(days=1)
    return out


def walk(dates, start_px=100.0, drift=0.0004, amp=0.006, phase=0, vol=100000):
    """Deterministic, consistent-OHLC series: [(date, o, h, l, c, v)]."""
    import math
    rows, px = [], start_px
    for i, d in enumerate(dates):
        o = px * (1 + 0.001 * math.sin((i + phase) * 1.7))
        c = o * (1 + drift + amp * math.sin((i + phase) * 0.9))
        h = max(o, c) * 1.002
        l = min(o, c) * 0.998
        rows.append((d, round(o, 4), round(h, 4), round(l, 4), round(c, 4), vol + i * 10))
        px = c
    return rows


def _fmt(v):
    return "" if v is None else str(v)


def write_csv(path, rows, kind="ETF", series="EQ"):
    """rows: tuples (date,o,h,l,c,v) — values may be None (empty cell) or raw strings ('Infinity')."""
    lines = [HDR_ETF if kind == "ETF" else HDR_IDX]
    for r in rows:
        if kind == "ETF":
            lines.append("%s,%s,%s,%s,%s,%s,%s,,," % (r[0], _fmt(r[1]), _fmt(r[2]), _fmt(r[3]), _fmt(r[4]), _fmt(r[5]), r[6] if len(r) > 6 else series))
        else:
            lines.append("%s,%s,%s,%s,%s,%s,20.5,,,," % (r[0], _fmt(r[1]), _fmt(r[2]), _fmt(r[3]), _fmt(r[4]), _fmt(r[5])))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def make_eod2(tmp_path, etfs, indices, holidays=None, year=2026, last_update="2026-03-10", special_meta=None, special_txt=None, isin=None):
    """etfs / indices: {file stem: rows}. Returns the data root (contains daily/, meta.json, ...)."""
    root = os.path.join(str(tmp_path), "eod2_data")
    os.makedirs(os.path.join(root, "daily"), exist_ok=True)
    for stem, rows in etfs.items():
        write_csv(os.path.join(root, "daily", stem + ".csv"), rows, "ETF")
    for stem, rows in indices.items():
        write_csv(os.path.join(root, "daily", stem + ".csv"), rows, "INDEX")
    meta = {"lastUpdate": last_update + "T00:00:00+05:30", "data-version": 3.4, "year": year,
            "holidays": holidays if holidays is not None else {"26-Jan-2026": "Republic Day", "14-Sep-2026": "Ganesh Chaturthi"},
            "special_sessions": special_meta or []}
    with open(os.path.join(root, "meta.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=2)
    with open(os.path.join(root, "special_sessions.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(special_txt or []) + ("\n" if special_txt else ""))
    with open(os.path.join(root, "isin_symbol_map.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"sym2isin": isin or {}}, f)
    return root


def make_registry(etfs=("AAA", "BBB", "CCC"), indices=("NIFTY 50", "NIFTY BANK"), default_start="2025-06-02", splits=(), moves=(),
                  quarantine=None, gap=None, overrides=None):
    inst = []
    for s in etfs:
        inst.append({"id": "NSE:" + s, "symbol": s, "kind": "ETF", "exchange": "NSE", "isin": "", "name": s, "assetClass": "equity",
                     "engines": ["LIFO", "DM"], "linkedIndex": None, "listingDate": None, "listingDateEvidence": None, "delistingDate": None,
                     "aliases": [], "eod2File": None, "historyStart": None, "enabled": True})
    idx = [{"id": "IDX:" + n, "name": n, "kind": "INDEX", "eod2File": None, "requiredFields": ["open", "high", "close"], "historyStart": None, "enabled": True}
           for n in indices]
    reg = {"version": 3, "defaultHistoryStart": default_start, "calendar": {"cutoffIST": "19:15", "holidaySource": "eod2_meta"},
           "instruments": inst, "indices": idx, "splits": list(splits), "genuineMoves": list(moves),
           "quarantine": quarantine if quarantine is not None else {}, "gapPolicy": gap if gap is not None else {"known_no_trade": {}, "gap_allow_all": {}},
           "legacyV2": {"jump_threshold": 0.15, "patches": {}}}
    for k, v in (overrides or {}).items():
        reg[k] = v
    return reg


def write_registry(tmp_path, reg, name="universe.json"):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(reg, f, indent=2)
    return p


def migrated_move(sym, d, note="real print"):
    return {"symbol": sym, "date": d, "evidence": None, "approvedAt": None, "provenance": "migrated-v2-2026-09-11", "note": note}


def dataops_move(sym, d, evidence="NSE circular 42"):
    return {"symbol": sym, "date": d, "evidence": evidence, "approvedAt": "2026-09-19T10:00:00+05:30", "provenance": "dataops"}


class World:
    """One consistent world: 320 weekdays from 2025-01-01 (minus two holidays), 3 ETFs + 2 indices, default window start 2025-06-02."""
    SKIP = {"2025-08-15", "2026-01-26"}

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.dates = weekdays("2025-01-01", 320, self.SKIP)
        self.latest = self.dates[-1]
        self.etf_rows = {s: walk(self.dates, 100 + 20 * i, phase=i * 3) for i, s in enumerate(["aaa", "bbb", "ccc"])}
        self.idx_rows = {"nifty 50": walk(self.dates, 20000, 0.0003, 0.004, 0), "nifty bank": walk(self.dates, 45000, 0.0004, 0.006, 5)}

    def build(self, **kw):
        etfs = copy.deepcopy(kw.pop("etf_rows", self.etf_rows))
        idxs = copy.deepcopy(kw.pop("idx_rows", self.idx_rows))
        return make_eod2(self.tmp, etfs, idxs, **kw)
