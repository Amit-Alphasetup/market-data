"""Read-only access to an EOD2 data folder: daily CSV bars (v2 column parsing ported), meta.json, ISIN map, special sessions."""
import csv
import json
import math
import os
from datetime import date, datetime

from . import schema_universe as su

BAD = object()   # sentinel value for cells that are non-empty but not a finite number
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def fnum(v):
    """v2 semantics for empty cells (None) but non-empty junk / NaN / Infinity is reported as BAD instead of silently dropped."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        x = float(s)
    except ValueError:
        return BAD
    return x if math.isfinite(x) else BAD


def parse_holiday_key(k):
    """'26-Jan-2026' -> '2026-01-26' (None if unparseable)."""
    try:
        d, m, y = k.split("-")
        return "%04d-%02d-%02d" % (int(y), _MONTHS[m[:3].title()], int(d))
    except (ValueError, KeyError):
        return None


class Eod2Source:
    """Layout: <data_root>/daily/<lowercase name>.csv, <data_root>/meta.json, isin_symbol_map.json, special_sessions.txt."""

    def __init__(self, data_root):
        self.data_root = data_root
        self.daily = os.path.join(data_root, "daily")
        if not os.path.isdir(self.daily):
            raise FileNotFoundError("EOD2 daily folder not found: " + self.daily)
        self._stems = None
        self._meta = None
        self._isin = None

    # -- files --------------------------------------------------------------------------------------------------------
    def stems(self):
        """{lowercase stem: [file names]} — case-insensitive candidate lookup."""
        if self._stems is None:
            idx = {}
            for fn in sorted(os.listdir(self.daily)):
                if fn.lower().endswith(".csv"):
                    idx.setdefault(fn[:-4].lower(), []).append(fn)
            self._stems = idx
        return self._stems

    def path(self, filename):
        return os.path.join(self.daily, filename)

    # -- meta ---------------------------------------------------------------------------------------------------------
    def meta(self):
        if self._meta is None:
            p = os.path.join(self.data_root, "meta.json")
            with open(p, encoding="utf-8") as f:
                self._meta = json.load(f)
        return self._meta

    def holidays(self):
        """({iso date: description}, year). EOD2 stores only the current calendar year."""
        m = self.meta()
        out = {}
        for k, v in (m.get("holidays") or {}).items():
            iso = parse_holiday_key(k)
            if iso:
                out[iso] = v
        return out, m.get("year")

    def special_sessions(self):
        """ISO dates of announced special live sessions (meta.special_sessions + special_sessions.txt)."""
        out = set()
        for x in self.meta().get("special_sessions") or []:
            out.add(str(x)[:10])
        p = os.path.join(self.data_root, "special_sessions.txt")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        out.add(ln[:10])
        return {d for d in out if su.is_date(d)}

    def last_update(self):
        v = self.meta().get("lastUpdate")
        return str(v)[:10] if v else None

    # -- ISIN ---------------------------------------------------------------------------------------------------------
    def isin_of(self, symbol):
        if self._isin is None:
            p = os.path.join(self.data_root, "isin_symbol_map.json")
            self._isin = {}
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    self._isin = (json.load(f) or {}).get("sym2isin", {}) or {}
        return self._isin.get(symbol)

    def stores_isin(self):
        return os.path.isfile(os.path.join(self.data_root, "isin_symbol_map.json"))

    # -- bars ---------------------------------------------------------------------------------------------------------
    def read_bars(self, filename):
        """Parse one daily CSV. Returns (rows, findings).
        rows: date-sorted dicts {date, open, high, low, close, volume, series}; values are float | None | BAD.
        findings: [{'code': 'INVALID_DATE'|'SCHEMA_INVALID', 'date'/'detail': ...}] (rows with an invalid date are skipped)."""
        rows, findings, seen = [], [], set()
        with open(self.path(filename), encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            cols = {c.strip().lower(): c for c in (rd.fieldnames or [])}
            if "date" not in cols or "close" not in cols:
                return [], [{"code": "SCHEMA_INVALID", "detail": "missing Date/Close column in " + filename}]
            for r in rd:
                raw = (r.get(cols["date"]) or "").strip()
                if raw == "":
                    continue
                d = raw[:10]
                if not su.is_date(d):
                    findings.append({"code": "INVALID_DATE", "date": raw})
                    continue
                if d in seen:
                    findings.append({"code": "SCHEMA_INVALID", "detail": "duplicate date " + d})
                    continue
                seen.add(d)
                g = lambda k: fnum(r.get(cols[k])) if k in cols else None  # noqa: E731
                rows.append({"date": d, "open": g("open"), "high": g("high"), "low": g("low"), "close": g("close"),
                             "volume": g("volume"), "series": (r.get(cols.get("series", ""), "") or "").strip()})
        rows.sort(key=lambda b: b["date"])
        return rows, findings


def weekday(d):
    return date.fromisoformat(d).weekday()


def add_days(d, n):
    from datetime import timedelta
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def utc_now_iso_ist(now=None):
    from datetime import timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return (now or datetime.now(ist)).astimezone(ist).isoformat(timespec="seconds")
