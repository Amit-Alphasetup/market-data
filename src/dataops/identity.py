"""Identity resolution (plan D1 step 3): registry instrument -> exactly one EOD2 file, verified.

ETF:   filename match (symbol or alias, case-insensitive) -> candidate. 0 -> NOT_IN_EOD2 | >1 -> AMBIGUOUS_SYMBOL.
       Candidate verified when the registry ISIN equals the ISIN EOD2 holds for the symbol; registry ISIN empty -> WARN ISIN_MISSING;
       mismatch (or EOD2 has no ISIN to compare) -> IDENTITY_MISMATCH. The latest bar's SERIES must be EQ/BE/BZ, else IDENTITY_MISMATCH.
INDEX: name match with space/underscore normalisation.
"""
from . import reasons as R

ETF_SERIES = ("EQ", "BE", "BZ")


def norm_name(s):
    return " ".join(str(s).lower().replace("_", " ").split())


def resolve_etf(inst, src):
    """-> (filename or None, [reason dicts]). Reasons may hold blocks (file None) and warns (file set)."""
    if inst.get("eod2File"):
        fn = inst["eod2File"]
        if not __import__("os").path.isfile(src.path(fn)):
            return None, [R.reason("NOT_IN_EOD2", detail="registry eod2File %s not found" % fn)]
        cands = [fn]
    else:
        names = [inst["symbol"]] + [a["symbol"] for a in inst.get("aliases", [])]
        cands = []
        for n in names:
            for fn in src.stems().get(n.lower(), []):
                if fn not in cands:
                    cands.append(fn)
        if not cands:
            return None, [R.reason("NOT_IN_EOD2", detail="no file for %s (or aliases) in EOD2 daily" % inst["symbol"])]
        if len(cands) > 1:
            return None, [R.reason("AMBIGUOUS_SYMBOL", detail="candidates: " + ", ".join(cands))]
    out = []
    isin = inst.get("isin") or ""
    if isin == "":
        out.append(R.reason("ISIN_MISSING"))
    else:
        theirs = src.isin_of(inst["symbol"])
        if theirs != isin:
            return None, [R.reason("IDENTITY_MISMATCH", detail="registry ISIN %s vs EOD2 %s" % (isin, theirs))]
    return cands[0], out


def check_series(inst, rows):
    """Series check on the latest bar (ETF only). -> [reason] (empty if fine)."""
    if not rows:
        return []
    s = (rows[-1].get("series") or "").strip().upper()
    if s and s not in ETF_SERIES:
        return [R.reason("IDENTITY_MISMATCH", detail="latest bar SERIES %r is not one of %s" % (s, ",".join(ETF_SERIES)))]
    return []


def resolve_index(idx, src):
    """-> (filename or None, [reason dicts])."""
    if idx.get("eod2File"):
        fn = idx["eod2File"]
        if not __import__("os").path.isfile(src.path(fn)):
            return None, [R.reason("NOT_IN_EOD2", detail="registry eod2File %s not found" % fn)]
        return fn, []
    want = norm_name(idx["name"])
    cands = []
    for stem, files in src.stems().items():
        if norm_name(stem) == want:
            for fn in files:
                if fn not in cands:
                    cands.append(fn)
    if not cands:
        return None, [R.reason("NOT_IN_EOD2", detail="no index file for %s in EOD2 daily" % idx["name"])]
    if len(cands) > 1:
        return None, [R.reason("AMBIGUOUS_SYMBOL", detail="candidates: " + ", ".join(cands))]
    return cands[0], []
