"""Conformance runner (Python) for clients/eod2_client.py — same protocol and normalization as node_runner.mjs."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "clients"))
from eod2_client import EOD2Client  # noqa: E402

CASES = r"C:\dev\fixtures\cases"


def make_fetch(case_dir, base_url, state):
    def fetch(url):
        rel = str(url)
        if not rel.startswith(base_url):
            raise RuntimeError("unexpected url " + url)
        rel = rel[len(base_url):].split("?")[0]
        if rel in state["overrides"]:
            path = state["overrides"][rel]
        elif rel == "data/manifest.json" and state["pointer"]:
            path = state["pointer"]
        else:
            path = os.path.join(case_dir, "root", rel)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()
    return fetch


def resolve(case_dir, f):
    return os.path.join(case_dir, f[1:]) if f.startswith("@") else os.path.join(case_dir, "root", f)


def fail_of(r):
    return {"ok": False, "code": r["code"]}


def run_case(case_dir):
    with open(os.path.join(case_dir, "case.json"), encoding="utf-8") as f:
        spec = json.load(f)
    state = {"pointer": None, "overrides": {}}
    client = EOD2Client(spec["baseUrl"], cache="memory", fetch=make_fetch(case_dir, spec["baseUrl"], state))
    handles, out = {}, []
    for s in spec["steps"]:
        op = s["op"]
        h = handles.get(s.get("on", "h"))
        if op == "open":
            r = client.open_snapshot()
            if isinstance(r, dict):
                out.append(fail_of(r))
                continue
            handles[s.get("as", "h")] = r
            out.append({"ok": True, "id": r.id, "sessions": len(r.calendar["sessions"])})
        elif op == "getSeries":
            r = h.get_series(s["symbol"], force=bool(s.get("force")))
            if r["ok"]:
                m = r["meta"]
                out.append({"ok": True, "bars": len(r["bars"]), "first": m["first"], "last": m["last"], "lastClose": r["bars"][-1][4], "cache": m["cacheStatus"],
                            "fromCache": m["fromCache"], "smallOhlc": m["smallOhlcDiscrepancies"]})
            else:
                out.append(fail_of(r))
        elif op == "checkCoverage":
            o = s.get("opts") or {}
            r = h.check_coverage(s["symbol"], from_=o.get("from"), to=o.get("to"), fields=o.get("fields"), warmup_sessions=o.get("warmupSessions", 0),
                                 mode=o.get("mode", "certified"), exec_dates=o.get("execDates"))
            out.append({"ok": r["ok"], "code": r["code"], "reasons": r["reasons"], "missing": r["missing"], "firstTradable": r["firstTradable"]})
        elif op == "verifyAll":
            r = h.verify_all(force=bool(s.get("force")))
            out.append({"ok": r["ok"], "checked": r["checked"], "failures": [[f["symbol"], f["code"]] for f in r["failures"]]})
        elif op == "switchPointer":
            state["pointer"] = resolve(case_dir, s["file"])
            out.append({"done": True})
        elif op == "serveOverride":
            state["overrides"][s["path"]] = resolve(case_dir, s["file"])
            out.append({"done": True})
        elif op == "corruptCache":
            out.append({"corrupted": client._test_corrupt_cache(s["symbol"])})
        elif op == "clearCache":
            o = s.get("opts") or {}
            out.append({"cleared": client.clear_cache(dataset_id=o.get("datasetId"), symbol=o.get("symbol"))})
        elif op == "cacheKeys":
            out.append({"keys": sorted("@sha:" + str(k).split(":")[2] for k in client._test_cache_keys())})
        else:
            raise RuntimeError("unknown op " + op)
    return out


def run_all(cases_dir=CASES):
    return {n: run_case(os.path.join(cases_dir, n)) for n in sorted(os.listdir(cases_dir))}
