"""Control-panel request contract (plan D3) — server-side validation. The panel (index.html) mirrors these rules; tests/data/request_cases.json
is run against BOTH validators so they cannot drift.

Request file: data/control/requests/<utcts>-<type>-<id8>.json      utcts = YYYYMMDDTHHMMSSZ, id8 = first 8 hex chars of `id`
  {"id": "<uuid4>", "type": "add", "createdAt": "<iso>", "createdBy": "panel", "args": {...}}
Nothing in a request is ever executed as shell text; every value is validated and passed to a fixed in-process function.
"""
import re
from datetime import datetime

from . import schema_universe as su

TYPES = ("run", "add", "remove", "set_engines", "backfill", "set_listing", "diagnose", "repair", "rebuild_missing", "approve_move", "add_split", "flag_bad_print")
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
FILE_RE = re.compile(r"^(\d{8}T\d{6}Z)-([a-z_]+)-([0-9a-f]{8})\.json\Z")
RATIO_RE = re.compile(r"^[1-9]\d{0,5}:[1-9]\d{0,5}\Z")
EVIDENCE_MAX = 500
# arg -> (required, kind)
SPEC = {
    "run": {},
    "add": {"symbol": (True, "symbol"), "kind": (False, "kind"), "engines": (False, "engines"), "assetClass": (False, "assetClass"), "isin": (False, "isin")},
    "remove": {"symbol": (True, "symbol")},
    "set_engines": {"symbol": (True, "symbol"), "engines": (True, "engines")},
    "backfill": {"symbol": (True, "symbol"), "from": (True, "date")},
    "set_listing": {"symbol": (True, "symbol"), "date": (True, "date"), "evidence": (True, "evidence")},
    "diagnose": {"symbol": (True, "symbol"), "from": (False, "date"), "to": (False, "date")},
    "repair": {"symbol": (True, "symbol"), "from": (False, "date"), "to": (False, "date")},
    "rebuild_missing": {},
    "approve_move": {"symbol": (True, "symbol"), "date": (True, "date"), "evidence": (True, "evidence")},
    "add_split": {"symbol": (True, "symbol"), "date": (True, "date"), "ratio": (True, "ratio"), "evidence": (True, "evidence")},
    "flag_bad_print": {"symbol": (True, "symbol"), "date": (True, "date"), "evidence": (True, "evidence")},
}
MUTATING_REGISTRY = ("add", "remove", "set_engines", "backfill", "set_listing", "approve_move", "add_split", "flag_bad_print")


def _check_value(kind, v):
    """-> error string or None"""
    if kind == "symbol":
        return None if isinstance(v, str) and su.SYMBOL_RE.match(v) else "symbol must match ^[A-Z0-9&-]{2,20}$"
    if kind == "date":
        return None if su.is_date(v) else "date must be a real YYYY-MM-DD date"
    if kind == "evidence":
        if not isinstance(v, str) or not v.strip():
            return "evidence is required (non-empty text)"
        if len(v) > EVIDENCE_MAX:
            return "evidence longer than %d characters" % EVIDENCE_MAX
        if any(ord(c) < 32 for c in v):
            return "evidence must not contain control characters"
        return None
    if kind == "engines":
        if not isinstance(v, list) or any(not isinstance(e, str) or e not in su.ENGINES for e in v) or len(set(v)) != len(v):
            return "engines must be a duplicate-free list drawn from %s" % (list(su.ENGINES),)
        return None
    if kind == "ratio":
        return None if isinstance(v, str) and RATIO_RE.match(v) else "ratio must look like 10:1 (positive integers)"
    if kind == "kind":
        return None if v == "ETF" else "kind must be ETF"
    if kind == "assetClass":
        return None if v in su.ASSET_CLASSES else "assetClass must be one of %s" % (list(su.ASSET_CLASSES),)
    if kind == "isin":
        return None if v == "" or (isinstance(v, str) and su.ISIN_RE.match(v)) else "isin must be empty or a 12-character ISIN"
    return "unknown arg kind"


def validate_request(req, filename=None):
    """-> list of error strings (empty = valid)."""
    errs = []
    if not isinstance(req, dict):
        return ["request must be a JSON object"]
    allowed = {"id", "type", "createdAt", "createdBy", "args"}
    for k in req:
        if k not in allowed:
            errs.append("unexpected key '%s'" % k)
    for k in ("id", "type", "createdAt", "createdBy", "args"):
        if k not in req:
            errs.append("missing key '%s'" % k)
    if errs:
        return errs
    if not isinstance(req["id"], str) or not UUID4_RE.match(req["id"]):
        errs.append("id must be a lowercase uuid4")
    if req["type"] not in TYPES:
        errs.append("unknown type %r" % (req["type"] if isinstance(req["type"], str) else type(req["type"]).__name__))
    if not isinstance(req["createdAt"], str) or not _iso(req["createdAt"]):
        errs.append("createdAt must be an ISO datetime")
    if req["createdBy"] != "panel":
        errs.append("createdBy must be 'panel'")
    args = req["args"]
    if not isinstance(args, dict):
        errs.append("args must be an object")
        return errs
    spec = SPEC.get(req["type"] if isinstance(req["type"], str) else "", None)
    if spec is not None:
        for k in args:
            if k not in spec:
                errs.append("unknown arg '%s' for type %s" % (k, req["type"]))
        for k, (required, kind) in spec.items():
            if k not in args:
                if required:
                    errs.append("missing arg '%s'" % k)
                continue
            e = _check_value(kind, args[k])
            if e:
                errs.append("%s: %s" % (k, e))
        if "from" in args and "to" in args and su.is_date(args.get("from")) and su.is_date(args.get("to")) and args["from"] > args["to"]:
            errs.append("from must not be after to")
    if filename is not None:
        m = FILE_RE.match(filename)
        if not m:
            errs.append("bad file name %r (expected <utcts>-<type>-<id8>.json)" % filename)
        elif isinstance(req.get("id"), str) and (m.group(2) != req.get("type") or m.group(3) != req["id"][:8]):
            errs.append("file name does not match the request's type/id")
    return errs


def _iso(s):
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False
