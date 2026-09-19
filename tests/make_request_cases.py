"""Generates tests/data/request_cases.json — HAND-SPECIFIED valid/invalid control-panel requests, run against BOTH validators
(Python: dataops.requests_spec, JS: index.html MDPanel.validateRequest). Re-run: py tests/make_request_cases.py"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dataops import requests_spec as rs  # noqa: E402

U = "3f2b8c1e-5d4a-4b6f-9a7c-0e1d2c3b4a59"
cases = []


def R(t="run", args=None, **kw):
    r = {"id": U, "type": t, "createdAt": "2026-03-10T14:11:07Z", "createdBy": "panel", "args": args if args is not None else {}}
    r.update(kw)
    return r


def F(t, uid=U):
    return "20260310T141107Z-%s-%s.json" % (t, uid[:8])


def add(name, req, valid, file=None, contains=None):
    c = {"name": name, "req": req, "valid": valid}
    if file is not None:
        c["file"] = file
    if contains:
        c["errorContains"] = contains
    cases.append(c)


# ── valid: every type ────────────────────────────────────────────────────────────────────────────────────────────────────
add("run", R("run"), True, F("run"))
add("add minimal", R("add", {"symbol": "NIFTYBEES"}), True, F("add"))
add("add full", R("add", {"symbol": "GOLDBEES", "kind": "ETF", "engines": ["LIFO", "DM"], "assetClass": "gold", "isin": "INF204KB17I5"}), True, F("add"))
add("add empty isin and engines", R("add", {"symbol": "AB", "engines": [], "isin": ""}), True)
add("remove", R("remove", {"symbol": "MID150BEES"}), True, F("remove"))
add("symbol with & and -", R("remove", {"symbol": "M&M-ETF"}), True)
add("set_engines", R("set_engines", {"symbol": "NIFTYBEES", "engines": ["LIFO", "DM", "TENET"]}), True, F("set_engines"))
add("set_engines empty (data-only)", R("set_engines", {"symbol": "NIFTYBEES", "engines": []}), True)
add("backfill", R("backfill", {"symbol": "NIFTYBEES", "from": "2019-01-14"}), True, F("backfill"))
add("set_listing", R("set_listing", {"symbol": "SILVERBEES", "date": "2022-02-07", "evidence": "NSE circular 123"}), True, F("set_listing"))
add("diagnose no dates", R("diagnose", {"symbol": "MONQ50"}), True, F("diagnose"))
add("diagnose range", R("diagnose", {"symbol": "MONQ50", "from": "2026-09-10", "to": "2026-09-18"}), True)
add("repair", R("repair", {"symbol": "MOM100", "from": "2022-05-11", "to": "2022-05-11"}), True, F("repair"))
add("rebuild_missing", R("rebuild_missing"), True, F("rebuild_missing"))
add("approve_move", R("approve_move", {"symbol": "MONQ50", "date": "2026-09-16", "evidence": "NSE price band circular"}), True, F("approve_move"))
add("add_split", R("add_split", {"symbol": "GOLD1", "date": "2021-07-22", "ratio": "10:1", "evidence": "NSE corporate action"}), True, F("add_split"))
add("leap day", R("backfill", {"symbol": "AB", "from": "2024-02-29"}), True)
add("evidence exactly 500 chars", R("set_listing", {"symbol": "AB", "date": "2020-01-01", "evidence": "x" * 500}), True)
add("uuid4 with variant 8", R("run", id="3f2b8c1e-5d4a-4b6f-8a7c-0e1d2c3b4a59"), True)
# ── invalid ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
add("unknown type", R("shutdown"), False, contains="unknown type")
add("type not a string", R(7), False)
add("unknown arg on run", R("run", {"cmd": "whoami"}), False, contains="unknown arg")
add("shell text as symbol", R("add", {"symbol": "NIFTY; del *"}), False, contains="symbol")
add("lowercase symbol", R("add", {"symbol": "niftybees"}), False, contains="symbol")
add("symbol too short", R("add", {"symbol": "A"}), False)
add("symbol too long", R("add", {"symbol": "A" * 21}), False)
add("symbol with space", R("remove", {"symbol": "NIFTY BEES"}), False)
add("symbol not a string", R("remove", {"symbol": 5}), False)
add("missing symbol", R("remove", {}), False, contains="missing arg")
add("missing evidence", R("approve_move", {"symbol": "MONQ50", "date": "2026-09-16"}), False, contains="missing arg")
add("blank evidence", R("approve_move", {"symbol": "MONQ50", "date": "2026-09-16", "evidence": "   "}), False, contains="evidence")
add("evidence too long", R("set_listing", {"symbol": "AB", "date": "2020-01-01", "evidence": "x" * 501}), False, contains="longer")
add("evidence with newline", R("set_listing", {"symbol": "AB", "date": "2020-01-01", "evidence": "a\nb"}), False, contains="control")
add("evidence not a string", R("set_listing", {"symbol": "AB", "date": "2020-01-01", "evidence": ["x"]}), False)
add("impossible date", R("backfill", {"symbol": "AB", "from": "2025-02-30"}), False, contains="date")
add("non-leap 29 feb", R("backfill", {"symbol": "AB", "from": "2023-02-29"}), False)
add("date wrong format", R("backfill", {"symbol": "AB", "from": "10-03-2026"}), False)
add("date single digit", R("backfill", {"symbol": "AB", "from": "2026-3-1"}), False)
add("diagnose from after to", R("diagnose", {"symbol": "AB", "from": "2026-09-18", "to": "2026-09-10"}), False, contains="after")
add("engines unknown", R("set_engines", {"symbol": "AB", "engines": ["LIFO", "XYZ"]}), False, contains="engines")
add("engines duplicate", R("set_engines", {"symbol": "AB", "engines": ["LIFO", "LIFO"]}), False)
add("engines string not list", R("set_engines", {"symbol": "AB", "engines": "LIFO,DM"}), False)
add("engines lowercase", R("set_engines", {"symbol": "AB", "engines": ["lifo"]}), False)
add("ratio without colon", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "10", "evidence": "x"}), False, contains="ratio")
add("ratio zero", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "0:1", "evidence": "x"}), False)
add("ratio zero denominator", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "10:0", "evidence": "x"}), False)
add("ratio letters", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "a:b", "evidence": "x"}), False)
add("ratio too big", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "1000000:1", "evidence": "x"}), False)
add("ratio negative", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "-2:1", "evidence": "x"}), False)
add("kind not ETF", R("add", {"symbol": "AB", "kind": "INDEX"}), False, contains="kind")
add("asset class invalid", R("add", {"symbol": "AB", "assetClass": "crypto"}), False, contains="assetClass")
add("isin invalid", R("add", {"symbol": "AB", "isin": "123"}), False, contains="isin")
add("createdBy not panel", R("run", createdBy="script"), False, contains="createdBy")
add("id not uuid", R("run", id="not-a-uuid"), False, contains="uuid4")
add("id uppercase", R("run", id=U.upper()), False)
add("id uuid v1", R("run", id="3f2b8c1e-5d4a-1b6f-9a7c-0e1d2c3b4a59"), False)
add("createdAt invalid", R("run", createdAt="yesterday"), False, contains="createdAt")
add("createdAt not a string", R("run", createdAt=12345), False)
add("extra top-level key", R("run", extra=1), False, contains="unexpected key")
add("missing args", {"id": U, "type": "run", "createdAt": "2026-03-10T14:11:07Z", "createdBy": "panel"}, False, contains="missing key")
add("args not an object", R("run", []), False, contains="args must be an object")
add("request is an array", [R("run")], False)
add("filename type mismatch", R("add", {"symbol": "AB"}), False, file=F("remove"), contains="does not match")
add("filename id mismatch", R("run"), False, file="20260310T141107Z-run-deadbeef.json", contains="does not match")
add("filename bad pattern", R("run"), False, file="run.json", contains="bad file name")
add("filename path traversal", R("run"), False, file="../../config/universe.json")
add("filename uppercase type", R("run"), False, file="20260310T141107Z-RUN-3f2b8c1e.json")

NL = chr(10)     # a trailing newline must never pass (Python's `$` would allow it; the validators use \Z / fullmatch)
add("symbol with trailing newline", R("remove", {"symbol": "AB" + NL}), False, contains="symbol")
add("ratio with trailing newline", R("add_split", {"symbol": "AB", "date": "2020-01-01", "ratio": "10:1" + NL, "evidence": "x"}), False, contains="ratio")
add("id with trailing newline", R("run", id=U + NL), False, contains="uuid4")
add("filename with trailing newline", R("run"), False, file=F("run") + NL, contains="bad file name")
add("isin with trailing newline", R("add", {"symbol": "AB", "isin": "INF204KB14I2" + NL}), False, contains="isin")
add("date with trailing newline", R("backfill", {"symbol": "AB", "from": "2026-01-01" + NL}), False, contains="date")

if __name__ == "__main__":
    bad = []
    for c in cases:
        errs = rs.validate_request(c["req"], c.get("file"))
        if (not errs) != c["valid"]:
            bad.append((c["name"], errs))
        if c.get("errorContains") and c["errorContains"] not in " | ".join(errs):
            bad.append((c["name"], "missing %r in %s" % (c["errorContains"], errs)))
    print(len(cases), "cases; mismatches:", bad)
    if bad:
        sys.exit(1)
    with open(os.path.join(ROOT, "tests", "data", "request_cases.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"cases": cases}, f, indent=1)
        f.write("\n")
