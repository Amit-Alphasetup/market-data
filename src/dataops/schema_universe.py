"""Registry (config/universe.json) validator — plan section 4.1. No external libraries.

validate(reg) -> list[str]   (empty list == valid).  Error strings start with a JSON-path-like prefix.
"""
import re
from datetime import date, datetime

ASSET_CLASSES = ("equity", "gold", "silver", "international", "debt", "liquid")
ENGINES = ("LIFO", "DM", "TENET")
INDEX_FIELDS = ("open", "high", "low", "close", "volume")
HOLIDAY_SOURCES = ("eod2_meta",)
MIGRATED_PROVENANCE = "migrated-v2-2026-09-11"
SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{2,20}\Z")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d\Z")

_TOP_REQUIRED = ("version", "defaultHistoryStart", "calendar", "instruments", "indices", "splits", "genuineMoves", "quarantine", "gapPolicy")
_TOP_OPTIONAL = ("legacyV2",)
_INSTR_KEYS = ("id", "symbol", "kind", "exchange", "isin", "name", "assetClass", "engines", "linkedIndex", "listingDate",
               "listingDateEvidence", "delistingDate", "aliases", "eod2File", "historyStart", "enabled")
_INDEX_KEYS = ("id", "name", "kind", "eod2File", "requiredFields", "historyStart", "enabled")


def is_date(v):
    """True for a YYYY-MM-DD string that is a real calendar date."""
    if not isinstance(v, str) or not DATE_RE.match(v):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def is_iso_datetime(v):
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def _check_keys(obj, required, path, errs, optional=()):
    if not isinstance(obj, dict):
        errs.append(f"{path}: must be an object")
        return False
    for k in required:
        if k not in obj:
            errs.append(f"{path}: missing key '{k}'")
    for k in obj:
        if k not in required and k not in optional:
            errs.append(f"{path}: unexpected key '{k}'")
    return True


def _check_approval(e, path, errs, symbols, kind):
    """Common well-formedness for splits[] / genuineMoves[] entries (per-date approvals)."""
    if not _nonempty_str(e.get("symbol")) or e.get("symbol") not in symbols:
        errs.append(f"{path}.symbol: must be a symbol present in instruments ({e.get('symbol')!r})")
    if not is_date(e.get("date")):
        errs.append(f"{path}.date: must be a real YYYY-MM-DD date ({e.get('date')!r})")
    ev = e.get("evidence")
    if ev is not None and not _nonempty_str(ev):
        errs.append(f"{path}.evidence: must be null or non-empty text")
    ap = e.get("approvedAt")
    if ap is not None and not is_iso_datetime(ap):
        errs.append(f"{path}.approvedAt: must be null or an ISO datetime ({ap!r})")
    prov = e.get("provenance")
    if not _nonempty_str(prov):
        errs.append(f"{path}.provenance: required")
    elif prov == "dataops":
        if ev is None:
            errs.append(f"{path}: provenance 'dataops' requires non-empty evidence")
        if ap is None:
            errs.append(f"{path}: provenance 'dataops' requires approvedAt")
    elif prov == MIGRATED_PROVENANCE:
        if ev is not None or ap is not None:
            errs.append(f"{path}: migrated entries must keep evidence/approvedAt null (never fabricated)")
    if kind == "split":
        for k in ("ratioNum", "ratioDen"):
            v = e.get(k)
            if not (isinstance(v, int) and not isinstance(v, bool) and v > 0):
                errs.append(f"{path}.{k}: must be a positive integer ({v!r})")


def validate(reg):
    errs = []
    if not _check_keys(reg, _TOP_REQUIRED, "$", errs, optional=_TOP_OPTIONAL):
        return errs
    if reg.get("version") != 3:
        errs.append("$.version: must be 3")
    if not is_date(reg.get("defaultHistoryStart")):
        errs.append("$.defaultHistoryStart: must be a real YYYY-MM-DD date")

    cal = reg.get("calendar")
    if _check_keys(cal, ("cutoffIST", "holidaySource"), "$.calendar", errs):
        if not (isinstance(cal.get("cutoffIST"), str) and TIME_RE.match(cal["cutoffIST"])):
            errs.append("$.calendar.cutoffIST: must be HH:MM")
        if cal.get("holidaySource") not in HOLIDAY_SOURCES:
            errs.append(f"$.calendar.holidaySource: must be one of {HOLIDAY_SOURCES}")

    symbols, ids = set(), set()
    insts = reg.get("instruments")
    if not isinstance(insts, list):
        errs.append("$.instruments: must be a list")
        insts = []
    for i, x in enumerate(insts):
        p = f"$.instruments[{i}]"
        if not _check_keys(x, _INSTR_KEYS, p, errs):
            continue
        sym = x.get("symbol")
        if not (isinstance(sym, str) and SYMBOL_RE.match(sym)):
            errs.append(f"{p}.symbol: must match {SYMBOL_RE.pattern} ({sym!r})")
        else:
            if sym in symbols:
                errs.append(f"{p}.symbol: duplicate symbol {sym}")
            symbols.add(sym)
        if x.get("kind") not in ("ETF", "EQ"):
            errs.append(f"{p}.kind: must be ETF or EQ")
        if x.get("exchange") != "NSE":
            errs.append(f"{p}.exchange: must be NSE")
        if x.get("id") != f"{x.get('exchange')}:{sym}":
            errs.append(f"{p}.id: must be '<exchange>:<symbol>' ({x.get('id')!r})")
        if x.get("id") in ids:
            errs.append(f"{p}.id: duplicate id {x.get('id')}")
        ids.add(x.get("id"))
        isin = x.get("isin")
        if not (isin == "" or (isinstance(isin, str) and ISIN_RE.match(isin))):
            errs.append(f"{p}.isin: must be '' or a 12-character ISIN ({isin!r})")
        if not _nonempty_str(x.get("name")):
            errs.append(f"{p}.name: required")
        if x.get("assetClass") not in ASSET_CLASSES:
            errs.append(f"{p}.assetClass: must be one of {ASSET_CLASSES} ({x.get('assetClass')!r})")
        eng = x.get("engines")
        if not isinstance(eng, list) or any(e not in ENGINES for e in eng) or len(set(eng)) != len(eng):
            errs.append(f"{p}.engines: must be a duplicate-free subset of {ENGINES} ({eng!r})")
        li = x.get("linkedIndex")
        if li is not None and not _nonempty_str(li):
            errs.append(f"{p}.linkedIndex: must be null or a name")
        ld = x.get("listingDate")
        if ld is not None and not is_date(ld):
            errs.append(f"{p}.listingDate: must be null or a real date")
        if ld is not None and not _nonempty_str(x.get("listingDateEvidence")):
            errs.append(f"{p}.listingDateEvidence: listingDate is evidence-only; evidence text required")
        if ld is None and x.get("listingDateEvidence") is not None:
            errs.append(f"{p}.listingDateEvidence: must be null when listingDate is null")
        dd = x.get("delistingDate")
        if dd is not None and not is_date(dd):
            errs.append(f"{p}.delistingDate: must be null or a real date")
        al = x.get("aliases")
        if not isinstance(al, list):
            errs.append(f"{p}.aliases: must be a list")
        else:
            for j, a in enumerate(al):
                if not (isinstance(a, dict) and SYMBOL_RE.match(str(a.get("symbol", ""))) and is_date(a.get("from")) and is_date(a.get("to"))):
                    errs.append(f"{p}.aliases[{j}]: must be {{symbol, from, to}} with real dates")
        ef = x.get("eod2File")
        if ef is not None and not _nonempty_str(ef):
            errs.append(f"{p}.eod2File: must be null or a file name")
        hs = x.get("historyStart")
        if hs is not None and not is_date(hs):
            errs.append(f"{p}.historyStart: must be null or a real date")
        if not isinstance(x.get("enabled"), bool):
            errs.append(f"{p}.enabled: must be true/false")

    names = set()
    idxs = reg.get("indices")
    if not isinstance(idxs, list):
        errs.append("$.indices: must be a list")
        idxs = []
    for i, x in enumerate(idxs):
        p = f"$.indices[{i}]"
        if not _check_keys(x, _INDEX_KEYS, p, errs):
            continue
        nm = x.get("name")
        if not _nonempty_str(nm):
            errs.append(f"{p}.name: required")
        else:
            if nm in names:
                errs.append(f"{p}.name: duplicate index {nm}")
            names.add(nm)
        if x.get("id") != f"IDX:{nm}":
            errs.append(f"{p}.id: must be 'IDX:<name>' ({x.get('id')!r})")
        if x.get("id") in ids:
            errs.append(f"{p}.id: duplicate id {x.get('id')}")
        ids.add(x.get("id"))
        if x.get("kind") != "INDEX":
            errs.append(f"{p}.kind: must be INDEX")
        rf = x.get("requiredFields")
        if not (isinstance(rf, list) and rf and all(f in INDEX_FIELDS for f in rf) and len(set(rf)) == len(rf)):
            errs.append(f"{p}.requiredFields: must be a non-empty duplicate-free subset of {INDEX_FIELDS}")
        ef = x.get("eod2File")
        if ef is not None and not _nonempty_str(ef):
            errs.append(f"{p}.eod2File: must be null or a file name")
        hs = x.get("historyStart")
        if hs is not None and not is_date(hs):
            errs.append(f"{p}.historyStart: must be null or a real date")
        if not isinstance(x.get("enabled"), bool):
            errs.append(f"{p}.enabled: must be true/false")

    for name, kind in (("splits", "split"), ("genuineMoves", "move")):
        lst = reg.get(name)
        if not isinstance(lst, list):
            errs.append(f"$.{name}: must be a list")
            continue
        seen = set()
        for i, e in enumerate(lst):
            p = f"$.{name}[{i}]"
            req = ("symbol", "date", "ratioNum", "ratioDen", "evidence", "approvedAt", "provenance") if kind == "split" \
                else ("symbol", "date", "evidence", "approvedAt", "provenance")
            if not _check_keys(e, req, p, errs, optional=("note",) if kind == "move" else ()):
                continue
            _check_approval(e, p, errs, symbols, kind)
            key = (e.get("symbol"), e.get("date"))
            if key in seen:
                errs.append(f"{p}: duplicate approval for {key[0]} on {key[1]} (approvals are per date)")
            seen.add(key)
            if "note" in e and not isinstance(e["note"], str):
                errs.append(f"{p}.note: must be text")

    q = reg.get("quarantine")
    if not isinstance(q, (dict, list)):
        errs.append("$.quarantine: must be an object (v2 verbatim) or list")
    if not isinstance(reg.get("gapPolicy"), dict):
        errs.append("$.gapPolicy: must be an object")
    if "legacyV2" in reg and not isinstance(reg["legacyV2"], dict):
        errs.append("$.legacyV2: must be an object")
    return errs


def assert_valid(reg):
    errs = validate(reg)
    if errs:
        raise ValueError("registry invalid:\n  " + "\n  ".join(errs))
