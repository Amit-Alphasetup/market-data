"""Series preparation + validation (plan D1 steps 4, 5, 7).

prepare(kind, item, rows, findings, registry) -> Prepared      (no calendar needed: schema, required fields, splits, window, OHLC, anomalies)
check_coverage(prep, item, registry, cal)      -> None         (needs the calendar: staleness, missing sessions, listing warnings)

`reasons` collects EVERY finding; the first block is the primary code. Blocks never abort the other checks so Apd sees the full picture.
"""
import math

from . import eod2_reader as er
from . import reasons as R

ETF_REQUIRED = ("open", "high", "low", "close")
PRICE_FIELDS = ("open", "high", "low", "close")
ROUND_SPLITS = (2, 3, 4, 5, 10, 20, 25, 50, 100)
IMPLIED_LO, IMPLIED_HI = 0.8, 1.25   # v2: declared split accepted when implied ratio within [0.8r, 1.25r]


class Prepared:
    def __init__(self, kind, key):
        self.kind = kind
        self.key = key                  # symbol (ETF) or index name
        self.reasons = []               # [{'code':..., ...}]
        self.rows = []                  # processed bars (dicts with float/None) inside the kept window
        self.first_observed = None      # first EOD2 date after leading incomplete rows are trimmed (before the window trim)
        self.events = []                # anomaly events for `quarantined`
        self.trimmed_leading = 0
        self.no_trade_days = []
        self.adjustments = []           # applied splits
        self.approvals_used = []
        self.quarantine_kind = False    # True when the block belongs in manifest.quarantined (anomaly / corporate action / registry)
        self.warmup_truncated = None    # {'from': date, 'events': [...]} when an unresolved pre-window anomaly cut the warm-up short

    def add(self, code, **detail):
        self.reasons.append(R.reason(code, **detail))

    @property
    def blocks(self):
        return [r for r in self.reasons if R.is_block(r["code"])]

    @property
    def warns(self):
        return [r for r in self.reasons if R.is_warn(r["code"])]

    @property
    def primary(self):
        b = self.blocks
        return b[0] if b else None

    @property
    def usable(self):
        return not self.blocks and bool(self.rows)

    @property
    def status(self):
        return "BLOCK" if self.blocks or not self.rows else ("WARN" if self.warns else "OK")


def _first(lst, n=5):
    return lst[:n]


def _ohlc_violation(b):
    """Relative size of the worst OHLC inconsistency in bar b (0 if consistent / not enough fields)."""
    o, h, l, c = b.get("open"), b.get("high"), b.get("low"), b.get("close")
    worst = 0.0
    present = [x for x in (o, c, l) if x is not None]
    if h is not None and present:
        worst = max(worst, (max(present) - h) / h if h > 0 else 0.0)
    present = [x for x in (o, c, h) if x is not None]
    if l is not None and present:
        worst = max(worst, (l - min(present)) / l if l > 0 else 0.0)
    return max(0.0, worst)


def _apply_splits(prep, rows, splits):
    """Port of v2 split handling, extended to ratioNum/ratioDen. Prices before the split date are divided by num/den, volume multiplied.
    A declared split whose implied ratio disagrees with the data (or whose date is missing) is CORPORATE_ACTION_UNRESOLVED."""
    for s in splits:
        ratio = s["ratioNum"] / s["ratioDen"]
        idx = next((i for i, b in enumerate(rows) if b["date"] >= s["date"]), None)
        if idx is None or idx == 0:
            prep.add("CORPORATE_ACTION_UNRESOLVED", date=s["date"], detail="declared split %d:%d, date not found inside the bars" % (s["ratioNum"], s["ratioDen"]))
            prep.quarantine_kind = True
            continue
        pc, c = rows[idx - 1]["close"], rows[idx]["close"]
        implied = pc / c if pc and c else 0
        if not (ratio * IMPLIED_LO <= implied <= ratio * IMPLIED_HI):
            prep.add("CORPORATE_ACTION_UNRESOLVED", date=s["date"],
                     detail="declared split %d:%d but implied %.2f:1" % (s["ratioNum"], s["ratioDen"], implied))
            prep.quarantine_kind = True
            continue
        for b in rows[:idx]:
            for k in PRICE_FIELDS:
                if b[k] is not None:
                    b[k] = b[k] / ratio
            if b["volume"] is not None:
                b["volume"] = b["volume"] * ratio
        prep.adjustments.append({"date": rows[idx]["date"], "ratioNum": s["ratioNum"], "ratioDen": s["ratioDen"], "implied": round(implied, 3)})


def prepare(kind, item, rows, findings, registry, src_isin_check=None):
    """kind: 'ETF' | 'INDEX'. item: registry instrument/index dict. rows: raw parsed rows (may contain eod2_reader.BAD)."""
    key = item["symbol"] if kind == "ETF" else item["name"]
    prep = Prepared(kind, key)
    inv_dates = [f["date"] for f in findings if f["code"] == "INVALID_DATE"]
    if inv_dates:
        prep.add("INVALID_DATE", count=len(inv_dates), dates=_first(inv_dates))
    for f in findings:
        if f["code"] == "SCHEMA_INVALID":
            prep.add("SCHEMA_INVALID", detail=f.get("detail"))
    fields = ("open", "high", "low", "close", "volume")
    bad = [(b["date"], k) for b in rows for k in fields if b[k] is er.BAD]
    if bad:
        prep.add("NONFINITE_VALUE", count=len(bad), first=[{"date": d, "field": k} for d, k in _first(bad)])
        return prep                       # numbers are unusable; nothing else can be checked reliably

    required = ETF_REQUIRED if kind == "ETF" else tuple(item["requiredFields"])
    # trim leading rows missing required fields (v2), later missing rows are errors
    i0 = next((i for i, b in enumerate(rows) if all(b[k] is not None for k in required)), None)
    if i0 is None:
        prep.add("MISSING_REQUIRED_FIELD", fields=list(required), detail="no bar has all required fields")
        return prep
    prep.trimmed_leading = i0
    rows = rows[i0:]
    prep.first_observed = rows[0]["date"]
    miss = [b["date"] for b in rows if any(b[k] is None for k in required)]
    if miss:
        prep.add("MISSING_REQUIRED_FIELD", fields=list(required), count=len(miss), dates=_first(miss))
    nonpos = [b["date"] for b in rows for k in PRICE_FIELDS if b[k] is not None and not b[k] > 0]
    if nonpos:
        prep.add("SCHEMA_INVALID", detail="non-positive price", count=len(nonpos), dates=_first(sorted(set(nonpos))))
    negv = [b["date"] for b in rows if b["volume"] is not None and b["volume"] < 0]
    if negv:
        prep.add("SCHEMA_INVALID", detail="negative volume", dates=_first(negv))

    if kind == "ETF":
        _apply_splits(prep, rows, registry.splits_for(prep.key))

    start = item.get("historyStart") or registry.default_start
    keep_from = start if item.get("historyStart") else er.add_days(start, -R.WARMUP_DAYS)
    rows = [b for b in rows if b["date"] >= keep_from]
    prep.rows = rows

    # anomaly gate FIRST (ETFs; indices cannot split — same as v2): an unresolved move BEFORE the window start (i.e. only inside the warm-up
    # range that v2 never looked at) truncates the warm-up right after it instead of quarantining an ETF whose window data is clean.
    if kind == "ETF":
        bad_dates = registry.bad_prints_on(prep.key)
        if bad_dates:
            rows = [b for b in rows if b["date"] not in bad_dates]
            prep.rows = rows
        approvals = registry.approvals_on(prep.key)
        pre_window, unresolved = [], []
        for i in range(1, len(rows)):
            p, c = rows[i - 1]["close"], rows[i]["close"]
            if not p or not c:
                continue
            ret = c / p - 1
            if abs(ret) < R.ANOMALY_THRESHOLD:
                continue
            d = rows[i]["date"]
            ev = {"date": d, "prevClose": round(p, 4), "close": round(c, 4), "ret": round(ret, 4)}
            a = approvals.get(d)
            if a is None:
                (pre_window if d < start else unresolved).append(ev)
            else:
                prep.approvals_used.append(d)
                if a.get("evidence") is None:
                    prep.add("APPROVAL_EVIDENCE_MISSING", date=d, provenance=a.get("provenance"))
        if pre_window:
            cut = pre_window[-1]["date"]
            prep.warmup_truncated = {"from": cut, "events": pre_window}
            rows = [b for b in rows if b["date"] >= cut]
            prep.rows = rows
        if unresolved:
            prep.events = unresolved
            prep.add("ANOMALOUS_MOVE_UNRESOLVED", events=unresolved)
            prep.quarantine_kind = True

    # OHLC consistency (rule 5)
    warn_dates, block_dates = [], []
    for b in rows:
        v = _ohlc_violation(b)
        if v > R.OHLC_WARN_TOLERANCE:
            block_dates.append((b["date"], v))
        elif v > 0:
            warn_dates.append(b["date"])
    if block_dates:
        prep.add("OHLC_INCONSISTENT", count=len(block_dates), first=[{"date": d, "violationPct": round(v * 100, 4)} for d, v in _first(block_dates)])
    if warn_dates:
        prep.add("SMALL_OHLC_DISCREPANCY", count=len(warn_dates), dates=_first(warn_dates))

    # registry quarantine
    q = registry.quarantine_reason(prep.key) if kind == "ETF" else None
    if q is not None:
        prep.add("EXPORT_FAILED", detail="REGISTRY_QUARANTINE: %s" % (q if isinstance(q, str) else "; ".join(map(str, q))))
        prep.quarantine_kind = True
    return prep


def check_coverage(prep, item, registry, cal, latest):
    """cal: {'sessions': set(regular sessions), 'windowStart': default start}. Adds STALE_DATASET / MISSING_EXPECTED_SESSION / listing reasons."""
    if not prep.rows:
        return
    kind = prep.kind
    last = prep.rows[-1]["date"]
    delisted = item.get("delistingDate") if kind == "ETF" else None
    window_start = registry.default_start
    if delisted and delisted < window_start:
        prep.add("DELISTED_BEFORE_WINDOW", delistingDate=delisted)
    elif not delisted and last < latest:
        prep.add("STALE_DATASET", last=last, expected=latest)
    listing = item.get("listingDate") if kind == "ETF" else None
    start = item.get("historyStart") or registry.default_start
    if listing:
        range_start = max(listing, start)
        if listing > window_start:
            prep.add("NOT_YET_LISTED", listingDate=listing)
    else:
        first_obs = prep.first_observed or prep.rows[0]["date"]
        if item.get("historyStart"):
            first_obs = max(first_obs, prep.rows[0]["date"])
        range_start = max(first_obs, start)
        if kind == "ETF" and first_obs > window_start:
            prep.add("LISTING_DATE_UNVERIFIED", firstObservedDate=first_obs)
    have = {b["date"] for b in prep.rows}
    end = min(last, delisted) if delisted else last
    expected = [d for d in sorted(cal["sessions"]) if range_start <= d <= end]
    missing = [d for d in expected if d not in have]
    if not missing:
        return
    sym = prep.key
    if kind == "ETF" and registry.gap_allow_all(sym):
        prep.no_trade_days = missing
        return
    allowed = registry.known_no_trade(sym) if kind == "ETF" else set()
    prep.no_trade_days = [d for d in missing if d in allowed]
    bad = [d for d in missing if d not in allowed]
    if bad:
        prep.add("MISSING_EXPECTED_SESSION", count=len(bad), dates=_first(bad))


def round_rows(rows):
    """Output rows: [date, o, h, l, c, volume] with prices rounded to 4 dp and integer volume (None stays null)."""
    out = []
    for b in rows:
        r = [b["date"]]
        for k in PRICE_FIELDS:
            v = b[k]
            r.append(None if v is None else round(v, 4))
        v = b["volume"]
        r.append(None if v is None else int(round(v)))
        out.append(r)
    return out


def nearest_round_split(ratio):
    return min(ROUND_SPLITS, key=lambda r: abs(math.log(ratio / r))) if ratio > 0 else None
