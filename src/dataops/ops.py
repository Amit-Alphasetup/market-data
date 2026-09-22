"""Registry-mutating commands (plan D2): add, remove, set-engines, backfill, set-listing, approve-move, add-split.

Each function loads config/universe.json, changes it, validates, saves, and regenerates the v2 config (gen_v2) — so the legacy job and
the v3 exporter always read the same registry. They are meant to run inside the dataops lock (the CLI does that); they never touch EOD2.
"""
import json
import os
from dataclasses import dataclass, field

from . import eod2_reader as er
from . import gen_v2 as g2
from . import migrate_v2 as mig
from . import schema_universe as su

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class Ctx:
    repo: str = REPO
    eod2_root: str = r"C:\dev\eod2\src\eod2_data"
    v2_out: str = g2.V2_PATH
    v2_backup: str = g2.V2_BACKUP
    now: object = None                  # datetime for tests
    extra: dict = field(default_factory=dict)

    @property
    def registry_path(self):
        return os.path.join(self.repo, "config", "universe.json")

    @property
    def work(self):
        return os.path.join(self.repo, "work")

    @property
    def snapshots(self):
        return os.path.join(self.repo, "data", "snapshots")


class OpError(Exception):
    """A refused operation (bad argument / not found). Message is user-facing."""


def now_iso(ctx):
    return er.utc_now_iso_ist(ctx.now)


def load(ctx):
    with open(ctx.registry_path, encoding="utf-8") as f:
        reg = json.load(f)
    su.assert_valid(reg)
    return reg


def save(ctx, reg):
    su.assert_valid(reg)
    with open(ctx.registry_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(mig.dumps(reg))
    g2.write_v2(reg, ctx.v2_out, ctx.v2_backup)


def _need_symbol(reg, symbol):
    symbol = str(symbol).upper()
    for x in reg["instruments"]:
        if x["symbol"] == symbol:
            return x
    raise OpError("%s is not in the registry" % symbol)


def _need_evidence(evidence):
    if not isinstance(evidence, str) or not evidence.strip():
        raise OpError("--evidence is required and must be non-empty text (NSE circular / corporate action reference)")
    return evidence.strip()


def _need_date(d, what="date"):
    if not su.is_date(d):
        raise OpError("%s must be a real YYYY-MM-DD date (got %r)" % (what, d))
    return d


def parse_engines(s):
    if s is None or s == "":
        return []
    eng = [e.strip().upper() for e in (s if isinstance(s, (list, tuple)) else str(s).split(",")) if str(e).strip()]
    bad = [e for e in eng if e not in su.ENGINES]
    if bad or len(set(eng)) != len(eng):
        raise OpError("engines must be a duplicate-free subset of %s (got %s)" % (list(su.ENGINES), s))
    return eng


def eod2_files_for(ctx, symbol):
    return er.Eod2Source(ctx.eod2_root).stems().get(symbol.lower(), [])


def op_add(ctx, symbol, kind="ETF", engines=None, asset_class=None, isin=""):
    symbol = str(symbol).upper()
    if not su.SYMBOL_RE.match(symbol):
        raise OpError("symbol must match %s" % su.SYMBOL_RE.pattern)
    if kind != "ETF":
        raise OpError("only kind ETF can be added (indices are configured in the registry)")
    reg = load(ctx)
    for x in reg["instruments"]:
        if x["symbol"] == symbol:
            if x["enabled"]:
                raise OpError("%s is already in the registry" % symbol)
            x["enabled"] = True
            save(ctx, reg)
            return "re-enabled %s" % symbol
    files = eod2_files_for(ctx, symbol)
    if not files:
        raise OpError("NOT_IN_EOD2: EOD2 only has symbols it downloaded; if newly listed wait for next sync")
    if len(files) > 1:
        raise OpError("AMBIGUOUS_SYMBOL: several EOD2 files match %s: %s" % (symbol, ", ".join(files)))
    ac = asset_class or mig.asset_class(symbol)
    if ac not in su.ASSET_CLASSES:
        raise OpError("asset class must be one of %s" % (su.ASSET_CLASSES,))
    reg["instruments"].append({
        "id": "NSE:" + symbol, "symbol": symbol, "kind": "ETF", "exchange": "NSE", "isin": isin or "", "name": symbol, "assetClass": ac,
        "engines": parse_engines(engines), "linkedIndex": None, "listingDate": None, "listingDateEvidence": None, "delistingDate": None,
        "aliases": [], "eod2File": None, "historyStart": None, "enabled": True})
    save(ctx, reg)
    return "added %s (%s, engines %s)" % (symbol, ac, ",".join(parse_engines(engines)) or "-")


def op_remove(ctx, symbol):
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    if not x["enabled"]:
        return "%s was already disabled" % x["symbol"]
    x["enabled"] = False
    save(ctx, reg)
    return "disabled %s (kept in the registry with its approvals)" % x["symbol"]


def op_set_engines(ctx, symbol, engines):
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    x["engines"] = parse_engines(engines)
    save(ctx, reg)
    return "%s engines = %s" % (x["symbol"], ",".join(x["engines"]) or "(none, data-only)")


def op_backfill(ctx, symbol, from_date):
    _need_date(from_date, "--from")
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    files = eod2_files_for(ctx, x["symbol"])
    first = None
    if len(files) == 1:
        rows, _ = er.Eod2Source(ctx.eod2_root).read_bars(files[0])
        first = next((r["date"] for r in rows if r["date"] >= from_date), None)
        earliest = rows[0]["date"] if rows else None
    else:
        earliest = None
    x["historyStart"] = from_date
    save(ctx, reg)
    msg = "%s historyStart = %s" % (x["symbol"], from_date)
    if earliest is not None:
        msg += " · EOD2's actual first bar is %s%s" % (earliest, "" if earliest <= from_date else " (LATER than requested: history before it does not exist in EOD2)")
    return msg


def op_set_listing(ctx, symbol, date, evidence):
    _need_date(date)
    ev = _need_evidence(evidence)
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    x["listingDate"], x["listingDateEvidence"] = date, ev
    save(ctx, reg)
    return "%s listingDate = %s (evidence recorded)" % (x["symbol"], date)


def _assert_date_not_claimed(reg, symbol, date, writing_to):
    """A single (symbol, date) may be recorded as a genuine move, a split, or a bad print — never
    more than one, since they mean contradictory things about the same printed bar. writing_to is the
    list this call is about to append to, so a fresh call into an empty list never trips on itself."""
    for name in ("genuineMoves", "splits", "badPrints"):
        if name == writing_to:
            continue
        for e in reg.get(name, []):
            if e["symbol"] == symbol and e["date"] == date:
                raise OpError("%s on %s is already recorded in %s — that record must be removed first if it was a mistake" % (symbol, date, name))


def op_approve_move(ctx, symbol, date, evidence):
    _need_date(date)
    ev = _need_evidence(evidence)
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    _assert_date_not_claimed(reg, x["symbol"], date, "genuineMoves")
    if any(m["symbol"] == x["symbol"] and m["date"] == date for m in reg["genuineMoves"]):
        raise OpError("a genuine-move approval for %s on %s already exists (approvals are per date)" % (x["symbol"], date))
    reg["genuineMoves"].append({"symbol": x["symbol"], "date": date, "evidence": ev, "approvedAt": now_iso(ctx), "provenance": "dataops"})
    save(ctx, reg)
    return "approved move %s on %s" % (x["symbol"], date)


def op_flag_bad_print(ctx, symbol, date, evidence):
    """The opposite of approve-move: this printed bar is not real (a vendor/exchange glitch, not a
    genuine price move and not a split), so it is dropped before it can trigger an anomaly at all —
    the date then reads exactly like a missing session (plan's existing MISSING_EXPECTED_SESSION rule),
    never a fabricated price. Other days for this ETF are unaffected."""
    _need_date(date)
    ev = _need_evidence(evidence)
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    _assert_date_not_claimed(reg, x["symbol"], date, "badPrints")
    reg.setdefault("badPrints", [])
    if any(m["symbol"] == x["symbol"] and m["date"] == date for m in reg["badPrints"]):
        raise OpError("a bad-print flag for %s on %s already exists (flags are per date)" % (x["symbol"], date))
    reg["badPrints"].append({"symbol": x["symbol"], "date": date, "evidence": ev, "approvedAt": now_iso(ctx), "provenance": "dataops"})
    save(ctx, reg)
    return "flagged %s on %s as a bad print — that day is now treated as if the session did not happen for this ETF" % (x["symbol"], date)


def parse_ratio(s):
    try:
        a, b = str(s).split(":")
        n, d = int(a), int(b)
    except ValueError:
        raise OpError("ratio must look like 10:1 (NUM:DEN, positive integers)")
    if n <= 0 or d <= 0:
        raise OpError("ratio parts must be positive integers")
    return n, d


def op_add_split(ctx, symbol, date, ratio, evidence):
    _need_date(date)
    ev = _need_evidence(evidence)
    n, d = parse_ratio(ratio)
    reg = load(ctx)
    x = _need_symbol(reg, symbol)
    _assert_date_not_claimed(reg, x["symbol"], date, "splits")
    if any(s["symbol"] == x["symbol"] and s["date"] == date for s in reg["splits"]):
        raise OpError("a split for %s on %s already exists" % (x["symbol"], date))
    reg["splits"].append({"symbol": x["symbol"], "date": date, "ratioNum": n, "ratioDen": d, "evidence": ev, "approvedAt": now_iso(ctx), "provenance": "dataops"})
    reg["splits"].sort(key=lambda s: (s["symbol"], s["date"]))
    save(ctx, reg)
    return "added split %s %d:%d on %s" % (x["symbol"], n, d, date)
