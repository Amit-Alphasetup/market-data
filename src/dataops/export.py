"""Exporter v3 — build_snapshot(): EOD2 folder + registry -> immutable snapshot folder (plan D1). Always called inside the dataops lock.

Three separate levels (never merged): publication integrity (publishStatus), instrument readiness (files[X].status + reasons /
excluded / quarantined), run certification (decided by the consuming app). One broken instrument NEVER blocks publication.
"""
import json
import os
import re

from . import CONTRACT_ID, VERSION
from . import calendar as cal_mod
from . import eod2_reader as er
from . import hashing as H
from . import identity as ident
from . import reasons as R
from . import validate as V
from .registry import Registry

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIELDS = ["date", "open", "high", "low", "close", "volume"]
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class SnapshotResult:
    def __init__(self):
        self.dataset_id = None
        self.out_dir = None
        self.manifest = None
        self.publish_status = "UNPUBLISHABLE"
        self.message = ""
        self.files = {}
        self.excluded = {}
        self.quarantined = {}
        self.summary = {}
        self.prepared = {}
        self.calendar = None
        self.warnings_listing = []      # ETFs with LISTING_DATE_UNVERIFIED (Q11)


def _compact(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def series_bytes(doc):
    """The exact bytes written and hashed for a series file."""
    return _compact(doc).encode("utf-8")


def next_dataset_id(latest, roots):
    """<latest>-r<N>, N = 1 + max existing N for that date across `roots` (must run inside the lock)."""
    pat = re.compile(r"^%s-r(\d+)$" % re.escape(latest))
    n = 0
    for root in roots:
        if root and os.path.isdir(root):
            for name in os.listdir(root):
                m = pat.match(name)
                if m:
                    n = max(n, int(m.group(1)))
    return "%s-r%d" % (latest, n + 1)


def validate_manifest(m):
    """Schema check of a snapshot manifest (plan 4.3). -> list of error strings."""
    errs = []
    req = ["contract", "dataset", "datasetId", "datasetHash", "policyHash", "snapshotBase", "createdAt", "exporterVersion", "latestSession",
           "latestCompletedSession", "windowStart", "publishStatus", "certification", "providerMixing", "fallbackUsed", "holidays", "calendar",
           "adjustmentPolicy", "files", "excluded", "quarantined", "summary"]
    for k in req:
        if k not in m:
            errs.append("manifest missing " + k)
    if errs:
        return errs
    if m["contract"] != CONTRACT_ID:
        errs.append("bad contract")
    if not SHA_RE.match(str(m["datasetHash"])) or not SHA_RE.match(str(m["policyHash"])):
        errs.append("bad hash format")
    if m["publishStatus"] not in ("PUBLISHABLE", "UNPUBLISHABLE"):
        errs.append("bad publishStatus")
    if m["certification"] not in ("PASS", "PARTIAL"):
        errs.append("bad certification")
    if m["providerMixing"] is not False or m["fallbackUsed"] is not False:
        errs.append("providerMixing/fallbackUsed must be false")
    c = m["calendar"]
    for k in ("id", "path", "sha256", "bytes", "cutoffIST", "coverageFrom", "coverageTo", "verifiedThrough"):
        if k not in c:
            errs.append("calendar.%s missing" % k)
    if not re.match(r"^\d{4}-\d{2}-\d{2}-r\d+$", str(m["datasetId"])) or m["snapshotBase"] != "snapshots/%s/" % m["datasetId"]:
        errs.append("datasetId/snapshotBase inconsistent")
    if not str(m["datasetId"]).startswith(str(m["latestCompletedSession"])):
        errs.append("datasetId date != latestCompletedSession")
    for k, f in m["files"].items():
        for fk in ("id", "kind", "path", "sha256", "bytes", "bars", "first", "last", "firstObservedDate", "fields", "status", "reasons"):
            if fk not in f:
                errs.append("files.%s missing %s" % (k, fk))
        if f.get("sha256") and not SHA_RE.match(f["sha256"]):
            errs.append("files.%s bad sha256" % k)
        if f.get("status") not in ("OK", "WARN"):
            errs.append("files.%s bad status" % k)
    s = m["summary"]
    if s.get("ok", 0) + s.get("warn", 0) != len(m["files"]) or s.get("excluded") != len(m["excluded"]) or s.get("quarantined") != len(m["quarantined"]):
        errs.append("summary inconsistent with files/excluded/quarantined")
    if s.get("requested") != len(m["files"]) + len(m["excluded"]) + len(m["quarantined"]):
        errs.append("summary.requested != files + excluded + quarantined")
    return errs


def _entry_status(reasons):
    return "WARN" if any(R.is_warn(r["code"]) for r in reasons) else "OK"


def verify_snapshot(out_dir, manifest):
    """Re-read the written bytes and check them against the manifest (used by the exporter and by the publisher). -> list of problems."""
    problems = []
    actual = {}
    for key, f in manifest["files"].items():
        path = os.path.join(out_dir, f["path"])
        if not os.path.isfile(path):
            problems.append("HASH_MISMATCH %s (file missing)" % key)
            continue
        actual[key] = {"sha256": H.sha256_file(path)}
        if actual[key]["sha256"] != f["sha256"]:
            problems.append("HASH_MISMATCH " + key)
        if os.path.getsize(path) != f["bytes"]:
            problems.append("HASH_MISMATCH %s (size)" % key)
    cal_path = os.path.join(out_dir, manifest["calendar"]["path"])
    cal_sha = H.sha256_file(cal_path) if os.path.isfile(cal_path) else None
    if cal_sha != manifest["calendar"]["sha256"]:
        problems.append("HASH_MISMATCH calendar")
    elif len(actual) == len(manifest["files"]) and H.dataset_hash(actual, cal_sha, manifest["policyHash"]) != manifest["datasetHash"]:
        problems.append("datasetHash mismatch")
    if os.path.isfile(cal_path):
        with open(cal_path, encoding="utf-8") as fh:
            problems += cal_mod.validate_calendar(json.load(fh))
    problems += validate_manifest(manifest)
    return problems


def build_snapshot(eod2_dir, config_path, out_root, now=None, existing_roots=None, exporter_version=VERSION):
    res = SnapshotResult()
    reg = Registry.load(config_path)
    src = er.Eod2Source(eod2_dir)
    items = []   # (kind, item, key, filename, pre_reasons, prep or None, block_reasons)

    # ── 3. resolve identity + 4/5. read, split-adjust, validate (calendar-independent) ───────────────────────────────────
    for kind, lst in (("ETF", reg.etfs), ("INDEX", reg.indices)):
        for it in lst:
            key = it["symbol"] if kind == "ETF" else it["name"]
            fn, rs = (ident.resolve_etf(it, src) if kind == "ETF" else ident.resolve_index(it, src))
            block = [r for r in rs if R.is_block(r["code"])]
            if fn is None or block:
                items.append((kind, it, key, fn, [], None, block or rs))
                continue
            rows, findings = src.read_bars(fn)
            prep = V.prepare(kind, it, rows, findings, reg)
            pre = list(rs)
            if kind == "ETF":
                for r in ident.check_series(it, rows):
                    prep.reasons.append(r)
            prep.reasons = pre + prep.reasons
            items.append((kind, it, key, fn, pre, prep, []))
            res.prepared[key] = prep

    # ── 6. latest completed session + calendar (from indices that passed the schema-level checks) ─────────────────────────
    parts = {key: p for kind, it, key, fn, pre, p, bl in items if kind == "INDEX" and p is not None and p.usable}
    if not parts:
        res.message = "no usable index: cannot build the session calendar (UNPUBLISHABLE)"
        res.excluded = {key: {"code": (bl[0]["code"] if bl else (p.primary["code"] if p and p.primary else "SCHEMA_INVALID"))} for kind, it, key, fn, pre, p, bl in items}
        return res
    counts = {}
    for p in parts.values():
        for b in p.rows:
            counts[b["date"]] = counts.get(b["date"], 0) + 1
    n = len(parts)
    majority = [d for d, c in counts.items() if c * 2 > n]
    if not majority:
        res.message = "indices share no common session date (UNPUBLISHABLE)"
        return res
    latest = max(majority)
    cal_key = "NIFTY 50" if "NIFTY 50" in parts else max(parts, key=lambda k: len(parts[k].rows))
    cal_dates = [b["date"] for b in parts[cal_key].rows if b["date"] <= latest]
    hol, hol_year = src.holidays()
    coverage_from = "%04d-01-01" % int(reg.default_start[:4])
    cal = cal_mod.build_calendar(cal_dates, hol, hol_year, src.special_sessions(), latest, reg.cutoff_ist, coverage_from)
    cal_errs = cal_mod.validate_calendar(cal)
    res.calendar = cal
    if cal_errs:
        res.message = "calendar invalid: " + "; ".join(cal_errs)
        return res
    sessions = set(cal["sessions"])

    # rows after the latest completed session are not part of this snapshot
    for kind, it, key, fn, pre, p, bl in items:
        if p is not None:
            p.rows = [b for b in p.rows if b["date"] <= latest]
            V.check_coverage(p, it, reg, {"sessions": sessions}, latest)

    res.dataset_id = next_dataset_id(latest, existing_roots if existing_roots is not None else [out_root, os.path.join(REPO, "data", "snapshots")])
    out_dir = os.path.join(out_root, res.dataset_id)
    res.out_dir = out_dir
    os.makedirs(os.path.join(out_dir, "etf"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "index"), exist_ok=True)

    # ── 8. write series files ──────────────────────────────────────────────────────────────────────────────────────────
    files, excluded, quarantined = {}, {}, {}
    for kind, it, key, fn, pre, p, bl in items:
        if p is None:
            excluded[key] = {"code": bl[0]["code"], "detail": bl[0].get("detail", ""), "reasons": bl}
            continue
        if not p.usable:
            blocks = p.blocks or [R.reason("EXPORT_FAILED", detail="no bars inside the window")]
            primary = blocks[0]
            if p.quarantine_kind:
                ev = p.events or [{"detail": r.get("detail")} for r in blocks if r.get("detail")]
                quarantined[key] = {"code": primary["code"], "events": ev, "reasons": blocks}
            else:
                excluded[key] = {"code": primary["code"], "detail": primary.get("detail") or json.dumps({k: v for k, v in primary.items() if k != "code"}, ensure_ascii=False),
                                 "reasons": blocks}
            continue
        isetf = kind == "ETF"
        sid = ("NSE:" + key) if isetf else ("IDX:" + key)
        doc = {"contract": CONTRACT_ID, "provider": "EOD2", "id": sid, "symbol": key, "kind": kind, "adjustment": reg.adjustment_policy,
               "fields": FIELDS, "bars": V.round_rows(p.rows)}
        rel = ("etf/%s.json" % key) if isetf else ("index/%s.json" % key.replace(" ", "_"))
        data = series_bytes(doc)
        with open(os.path.join(out_dir, rel), "wb") as f:
            f.write(data)
        rs = [r for r in p.reasons if not R.is_block(r["code"])]
        entry = {"id": sid, "kind": kind, "path": rel, "sha256": H.sha256_bytes(data), "bytes": len(data), "bars": len(doc["bars"]),
                 "first": doc["bars"][0][0], "last": doc["bars"][-1][0], "firstObservedDate": p.first_observed,
                 "listingDate": it.get("listingDate") if isetf else None,
                 "listingDateSource": "registry-evidence" if (isetf and it.get("listingDate")) else "none",
                 "fields": FIELDS, "assetClass": it["assetClass"] if isetf else None, "engines": list(it["engines"]) if isetf else [],
                 "linkedIndex": it.get("linkedIndex") if isetf else None, "isin": it.get("isin", "") if isetf else "",
                 "status": _entry_status(rs), "reasons": rs}
        if p.no_trade_days:
            entry["noTradeDays"] = p.no_trade_days
        if p.warmup_truncated:
            entry["warmupTruncatedFrom"] = p.warmup_truncated["from"]
        files[key] = entry
        if any(r["code"] == "LISTING_DATE_UNVERIFIED" for r in rs) and isetf:
            res.warnings_listing.append(key)

    # ── calendar file, hashes, manifest ────────────────────────────────────────────────────────────────────────────────
    cal_bytes = cal_mod.dumps(cal).encode("utf-8")
    with open(os.path.join(out_dir, "calendar.json"), "wb") as f:
        f.write(cal_bytes)
    cal_sha = H.sha256_bytes(cal_bytes)
    d = reg.data
    pol = H.policy_hash(reg.adjustment_policy, d["splits"], d["genuineMoves"], d["quarantine"], d["gapPolicy"])
    dh = H.dataset_hash(files, cal_sha, pol)
    n_ok = sum(1 for f in files.values() if f["status"] == "OK")
    n_warn = len(files) - n_ok
    manifest = {
        "contract": CONTRACT_ID, "dataset": "EOD2", "datasetId": res.dataset_id, "datasetHash": dh, "policyHash": pol,
        "snapshotBase": "snapshots/%s/" % res.dataset_id, "createdAt": er.utc_now_iso_ist(now), "exporterVersion": exporter_version,
        "latestSession": latest, "latestCompletedSession": latest, "windowStart": reg.default_start,
        "publishStatus": "PUBLISHABLE", "certification": "PASS" if not (excluded or quarantined) else "PARTIAL",
        "providerMixing": False, "fallbackUsed": False, "holidays": cal["holidays"],
        "calendar": {"id": cal["id"], "path": "calendar.json", "sha256": cal_sha, "bytes": len(cal_bytes), "cutoffIST": cal["cutoffIST"],
                     "coverageFrom": cal["coverageFrom"], "coverageTo": cal["coverageTo"], "verifiedThrough": cal["verifiedThrough"]},
        "adjustmentPolicy": reg.adjustment_policy, "files": files, "excluded": excluded, "quarantined": quarantined,
        "summary": {"requested": len(reg.etfs) + len(reg.indices), "ok": n_ok, "warn": n_warn, "excluded": len(excluded), "quarantined": len(quarantined)},
    }

    # ── 10. self-verify: re-read bytes, recompute every sha256 + datasetHash, re-validate the schema ──────────────────────
    problems = verify_snapshot(out_dir, manifest)
    if not files:
        problems.append("zero instruments usable")
    if problems:
        manifest["publishStatus"] = "UNPUBLISHABLE"
        res.message = "; ".join(problems)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    report = {"datasetId": res.dataset_id, "generatedBy": "dataops %s" % exporter_version, "summary": manifest["summary"],
              "publishStatus": manifest["publishStatus"], "listingDateUnverified": sorted(res.warnings_listing),
              "instruments": {k: {"status": f["status"], "reasons": [r["code"] for r in f["reasons"]]} for k, f in files.items()},
              "excluded": {k: v["code"] for k, v in excluded.items()}, "quarantined": {k: v["code"] for k, v in quarantined.items()}}
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_compact(report))
    res.manifest, res.publish_status, res.files, res.excluded, res.quarantined, res.summary = manifest, manifest["publishStatus"], files, excluded, quarantined, manifest["summary"]
    return res
