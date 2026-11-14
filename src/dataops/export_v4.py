"""Exporter v4 (contract alphadesk-eod2/4, Backtest Lab plan r2 section 5) - ETFs + indices + stocks, published as ONE replaceable tree:

    manifest.json                       the only fixed name
    cal/<sha12>/calendar.json
    hist/<KIND>/<KEY>.<sha12>.json      series document, bars up to histEnd (31 Dec of the year before the latest session); content-addressed
    tail/<KIND>.<datasetId>.json        {"kind","datasetId","from","series":{KEY:[[date,o,h,l,c,v],..]}} - the current year, every symbol of a kind

Reuses the v3 steps (identity, validate.prepare, check_coverage, calendar); v3's build_snapshot is untouched.
ETF and INDEX series carry exactly the v3 window (same bars as the v3 snapshot of the same day), so AlphaDesk sees identical data on either
contract. Stocks (kind EQ) carry everything from the registry floor (validate.prepare, EQ rules).
"""
import json
import os

from . import VERSION
from . import calendar as cal_mod
from . import eod2_reader as er
from . import hashing as H
from . import identity as ident
from . import reasons as R
from . import validate as V
from .export import FIELDS, SHA_RE, _compact, _entry_status, series_bytes
from .registry import Registry

CONTRACT_V4 = "alphadesk-eod2/4"
ADJUSTMENT_V4 = "eod2-split-bonus+reviewed-actions"
KINDS = ("EQ", "ETF", "INDEX")


class V4Result:
    def __init__(self):
        self.dataset_id = None
        self.out_dir = None
        self.manifest = None
        self.publish_status = "UNPUBLISHABLE"
        self.message = ""
        self.pending = {}         # EQ symbol -> unapproved overnight-gap events (history starts at the last one)
        self.total_bytes = 0


def policy_hash_v4(reg):
    d = reg.data
    return H.sha256_bytes(H.canonical_json({
        "adjustmentPolicy": ADJUSTMENT_V4, "splits": d["splits"], "genuineMoves": d["genuineMoves"], "quarantine": d["quarantine"],
        "gapPolicy": d["gapPolicy"], "badPrints": d.get("badPrints", []), "actions": d.get("actions", []),
        "eqHistoryFloor": reg.eq_floor, "eqGapThreshold": V.EQ_GAP_THRESHOLD}))


def dataset_hash_v4(files, tails, calendar_sha256, policy_hash_hex):
    """sha256 over the sorted (symbol, hist sha) pairs, the tail hashes, the calendar hash and the policy hash."""
    lines = sorted("hist:%s:%s" % (k, (f["hist"] or {}).get("sha256", "-")) for k, f in files.items())
    lines += sorted("tail:%s:%s" % (k, t["sha256"]) for k, t in tails.items())
    lines += ["calendar:%s" % calendar_sha256, "policy:%s" % policy_hash_hex, "contract:" + CONTRACT_V4]
    return H.sha256_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def hist_end_for(latest):
    return "%04d-12-31" % (int(latest[:4]) - 1)


def validate_manifest_v4(m):
    errs = []
    req = ["contract", "dataset", "datasetId", "datasetHash", "policyHash", "histEnd", "createdAt", "exporterVersion", "latestSession",
           "latestCompletedSession", "windowStart", "eqHistoryFloor", "publishStatus", "certification", "providerMixing", "fallbackUsed",
           "holidays", "calendar", "adjustmentPolicy", "tails", "files", "excluded", "quarantined", "pendingActions", "summary"]
    for k in req:
        if k not in m:
            errs.append("manifest missing " + k)
    if errs:
        return errs
    if m["contract"] != CONTRACT_V4:
        errs.append("bad contract")
    if not SHA_RE.match(str(m["datasetHash"])) or not SHA_RE.match(str(m["policyHash"])):
        errs.append("bad hash format")
    if m["publishStatus"] not in ("PUBLISHABLE", "UNPUBLISHABLE") or m["certification"] not in ("PASS", "PARTIAL"):
        errs.append("bad publishStatus/certification")
    if m["providerMixing"] is not False or m["fallbackUsed"] is not False:
        errs.append("providerMixing/fallbackUsed must be false")
    if m["histEnd"] != hist_end_for(m["latestCompletedSession"]) or not str(m["datasetId"]).startswith(str(m["latestCompletedSession"])):
        errs.append("histEnd/datasetId inconsistent with latestCompletedSession")
    for k in ("id", "path", "sha256", "bytes", "cutoffIST", "coverageFrom", "coverageTo", "verifiedThrough"):
        if k not in m["calendar"]:
            errs.append("calendar.%s missing" % k)
    for kind, t in m["tails"].items():
        if kind not in KINDS or not SHA_RE.match(str(t.get("sha256"))) or t.get("path") != "tail/%s.%s.json" % (kind, m["datasetId"]):
            errs.append("tails.%s malformed" % kind)
    for k, f in m["files"].items():
        for fk in ("id", "kind", "hist", "tailBars", "bars", "first", "last", "fields", "status", "reasons"):
            if fk not in f:
                errs.append("files.%s missing %s" % (k, fk))
                break
        else:
            h = f["hist"]
            if h is not None:
                if not SHA_RE.match(str(h.get("sha256"))) or h.get("path") != "hist/%s/%s.%s.json" % (f["kind"], k, h["sha256"][:12]):
                    errs.append("files.%s hist path is not content-addressed" % k)
                if h.get("last", "") > m["histEnd"]:
                    errs.append("files.%s hist reaches past histEnd" % k)
            if f["bars"] != (h["bars"] if h else 0) + f["tailBars"] or f["bars"] <= 0:
                errs.append("files.%s bar counts inconsistent" % k)
            if f["kind"] not in m["tails"] and f["tailBars"]:
                errs.append("files.%s has tail bars but kind %s has no tail" % (k, f["kind"]))
            if f.get("status") not in ("OK", "WARN"):
                errs.append("files.%s bad status" % k)
    s = m["summary"]
    if s.get("ok", 0) + s.get("warn", 0) != len(m["files"]) or s.get("excluded") != len(m["excluded"]) or s.get("quarantined") != len(m["quarantined"]):
        errs.append("summary inconsistent with files/excluded/quarantined")
    if s.get("requested") != len(m["files"]) + len(m["excluded"]) + len(m["quarantined"]):
        errs.append("summary.requested != files + excluded + quarantined")
    return errs


def verify_v4(out_dir, manifest):
    """Re-read every written byte and check it against the manifest (exporter self-check and publisher gate). -> list of problems."""
    problems = list(validate_manifest_v4(manifest))
    if problems:
        return problems

    def check(rel, sha, size, label):
        p = os.path.join(out_dir, *rel.split("/"))
        if not os.path.isfile(p):
            problems.append("HASH_MISMATCH %s (file missing)" % label)
            return None
        if H.sha256_file(p) != sha or os.path.getsize(p) != size:
            problems.append("HASH_MISMATCH " + label)
        return p

    tail_series = {}
    for kind, t in manifest["tails"].items():
        p = check(t["path"], t["sha256"], t["bytes"], "tail " + kind)
        if p:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            if doc.get("kind") != kind or doc.get("datasetId") != manifest["datasetId"]:
                problems.append("tail %s header mismatch" % kind)
            tail_series[kind] = doc.get("series", {})
    for key, f in manifest["files"].items():
        h = f["hist"]
        if h is not None:
            check(h["path"], h["sha256"], h["bytes"], key)
        tb = tail_series.get(f["kind"], {}).get(key, [])
        if len(tb) != f["tailBars"]:
            problems.append("tail bars of %s: manifest %d, file %d" % (key, f["tailBars"], len(tb)))
        elif tb and (tb[0][0] <= manifest["histEnd"] or tb[-1][0] != f["last"]):
            problems.append("tail of %s is outside (histEnd, last]" % key)
    for kind, ser in tail_series.items():
        extra = [k for k in ser if k not in manifest["files"]]
        if extra:
            problems.append("tail %s holds symbols that are not in files: %s" % (kind, ", ".join(extra[:3])))
    c = manifest["calendar"]
    cp = check(c["path"], c["sha256"], c["bytes"], "calendar")
    if cp:
        with open(cp, encoding="utf-8") as fh:
            problems += cal_mod.validate_calendar(json.load(fh))
    for name, r in (manifest.get("ref") or {}).items():
        if r.get("path") != "ref/%s.json" % name:
            problems.append("ref %s has an unexpected path" % name)
        else:
            check(r["path"], r["sha256"], r["bytes"], "ref " + name)
    if not problems and dataset_hash_v4(manifest["files"], manifest["tails"], c["sha256"], manifest["policyHash"]) != manifest["datasetHash"]:
        problems.append("datasetHash mismatch")
    return problems


def prepare_all(reg, src):
    """-> items [(kind, registry item, key, prepared or None, block reasons)] for every enabled ETF, stock and index."""
    items = []
    for kind, lst in (("ETF", reg.etfs), ("EQ", reg.stocks), ("INDEX", reg.indices)):
        for it in lst:
            key = it["name"] if kind == "INDEX" else it["symbol"]
            fn, rs = (ident.resolve_index(it, src) if kind == "INDEX" else ident.resolve_etf(it, src))
            block = [r for r in rs if R.is_block(r["code"])]
            if fn is None or block:
                items.append((kind, it, key, None, block or rs))
                continue
            rows, findings = src.read_bars(fn)
            prep = V.prepare(kind, it, rows, findings, reg)
            if kind != "INDEX":
                prep.reasons += ident.check_series(it, rows)
            prep.reasons = list(rs) + prep.reasons
            items.append((kind, it, key, prep, []))
    return items


def build_v4(eod2_dir, config_path, out_dir, now=None, revision=1, exporter_version=VERSION):
    """Writes the complete v4 tree into out_dir (must be empty or absent). Returns V4Result."""
    res = V4Result()
    reg = Registry.load(config_path)
    src = er.Eod2Source(eod2_dir)
    items = prepare_all(reg, src)

    parts = {key: p for kind, it, key, p, bl in items if kind == "INDEX" and p is not None and p.usable}
    if not parts:
        res.message = "no usable index: cannot build the session calendar (UNPUBLISHABLE)"
        return res
    counts = {}
    for p in parts.values():
        for b in p.rows:
            counts[b["date"]] = counts.get(b["date"], 0) + 1
    majority = [d for d, c in counts.items() if c * 2 > len(parts)]
    if not majority:
        res.message = "indices share no common session date (UNPUBLISHABLE)"
        return res
    latest = max(majority)
    cal_key = "NIFTY 50" if "NIFTY 50" in parts else max(parts, key=lambda k: len(parts[k].rows))
    cal_dates = [b["date"] for b in parts[cal_key].rows if b["date"] <= latest]
    hol, hol_year = src.holidays()
    cal = cal_mod.build_calendar(cal_dates, hol, hol_year, src.special_sessions(), latest, reg.cutoff_ist, "%04d-01-01" % int(reg.default_start[:4]))
    cal_errs = cal_mod.validate_calendar(cal)
    if cal_errs:
        res.message = "calendar invalid: " + "; ".join(cal_errs)
        return res
    # stocks reach back before the published calendar: their no-trade days are judged against every session the calendar index has
    all_sessions = set(cal_dates) | set(cal["sessions"])
    cal_sessions = set(cal["sessions"])
    for kind, it, key, p, bl in items:
        if p is not None:
            p.rows = [b for b in p.rows if b["date"] <= latest]
            V.check_coverage(p, it, reg, {"sessions": all_sessions if kind == "EQ" else cal_sessions}, latest)

    res.dataset_id = "%s-r%d" % (latest, revision)
    res.out_dir = out_dir
    hist_end = hist_end_for(latest)
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        res.message = "output folder %s is not empty" % out_dir
        return res
    for kind in KINDS:
        os.makedirs(os.path.join(out_dir, "hist", kind), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "tail"), exist_ok=True)

    files, excluded, quarantined, pending = {}, {}, {}, {}
    tail_series = {k: {} for k in KINDS}
    total = 0
    for kind, it, key, p, bl in items:
        if p is None:
            excluded[key] = {"code": bl[0]["code"], "detail": bl[0].get("detail", ""), "kind": kind, "reasons": bl}
            continue
        if not p.usable:
            blocks = p.blocks or [R.reason("EXPORT_FAILED", detail="no bars inside the window")]
            primary = blocks[0]
            if p.quarantine_kind:
                ev = p.events or [{"detail": r.get("detail")} for r in blocks if r.get("detail")]
                quarantined[key] = {"code": primary["code"], "kind": kind, "events": ev, "reasons": blocks}
            else:
                excluded[key] = {"code": primary["code"], "kind": kind, "reasons": blocks,
                                 "detail": primary.get("detail") or json.dumps({k: v for k, v in primary.items() if k != "code"}, ensure_ascii=False)}
            continue
        sid = ("IDX:" + key) if kind == "INDEX" else ("NSE:" + key)
        bars = V.round_rows(p.rows)
        hb = [b for b in bars if b[0] <= hist_end]
        tb = bars[len(hb):]
        hist = None
        if hb:
            doc = {"contract": CONTRACT_V4, "provider": "EOD2", "id": sid, "symbol": key, "kind": kind, "adjustment": ADJUSTMENT_V4, "fields": FIELDS, "bars": hb}
            data = series_bytes(doc)
            sha = H.sha256_bytes(data)
            rel = "hist/%s/%s.%s.json" % (kind, key, sha[:12])
            with open(os.path.join(out_dir, "hist", kind, "%s.%s.json" % (key, sha[:12])), "wb") as f:
                f.write(data)
            total += len(data)
            hist = {"path": rel, "sha256": sha, "bytes": len(data), "bars": len(hb), "first": hb[0][0], "last": hb[-1][0]}
        if tb:
            tail_series[kind][key] = tb
        rs = [r for r in p.reasons if not R.is_block(r["code"])]
        inst = kind != "INDEX"
        entry = {"id": sid, "kind": kind, "hist": hist, "tailBars": len(tb), "bars": len(bars), "first": bars[0][0], "last": bars[-1][0],
                 "firstObservedDate": p.first_observed, "listingDate": it.get("listingDate") if inst else None,
                 "listingDateSource": "registry-evidence" if (inst and it.get("listingDate")) else "none",
                 "fields": FIELDS, "assetClass": it["assetClass"] if inst else None, "engines": list(it["engines"]) if inst else [],
                 "linkedIndex": it.get("linkedIndex") if inst else None, "isin": it.get("isin", "") if inst else "",
                 "name": it.get("name", key), "status": _entry_status(rs), "reasons": rs}
        if p.no_trade_days:
            entry["noTradeDays"] = p.no_trade_days
        if p.warmup_truncated:
            entry["warmupTruncatedFrom"] = p.warmup_truncated["from"]
        if p.history_truncated:
            entry["historyTruncatedFrom"] = p.history_truncated["from"]
            pending[key] = p.history_truncated["events"]
        if p.adjustments:
            entry["adjustments"] = p.adjustments
        files[key] = entry

    tails = {}
    for kind in KINDS:
        if not any(f["kind"] == kind for f in files.values()):
            continue
        doc = {"kind": kind, "datasetId": res.dataset_id, "from": er.add_days(hist_end, 1),
               "series": {k: tail_series[kind][k] for k in sorted(tail_series[kind])}}
        data = _compact(doc).encode("utf-8")
        rel = "tail/%s.%s.json" % (kind, res.dataset_id)
        with open(os.path.join(out_dir, "tail", "%s.%s.json" % (kind, res.dataset_id)), "wb") as f:
            f.write(data)
        total += len(data)
        tails[kind] = {"path": rel, "sha256": H.sha256_bytes(data), "bytes": len(data), "symbols": len(doc["series"])}

    cal_bytes = cal_mod.dumps(cal).encode("utf-8")
    cal_sha = H.sha256_bytes(cal_bytes)
    cal_rel = "cal/%s/calendar.json" % cal_sha[:12]
    os.makedirs(os.path.join(out_dir, "cal", cal_sha[:12]), exist_ok=True)
    with open(os.path.join(out_dir, "cal", cal_sha[:12], "calendar.json"), "wb") as f:
        f.write(cal_bytes)
    total += len(cal_bytes)
    ref = {}
    mem_src = os.path.join(os.path.dirname(os.path.abspath(config_path)), "membership.json")
    if os.path.isfile(mem_src):  # P6: point-in-time index membership (scripts/build_membership.py), published as-is
        with open(mem_src, "rb") as f:
            mem_bytes = f.read()
        os.makedirs(os.path.join(out_dir, "ref"), exist_ok=True)
        with open(os.path.join(out_dir, "ref", "membership.json"), "wb") as f:
            f.write(mem_bytes)
        total += len(mem_bytes)
        ref["membership"] = {"path": "ref/membership.json", "sha256": H.sha256_bytes(mem_bytes), "bytes": len(mem_bytes)}
    pol = policy_hash_v4(reg)
    by_kind = {k: {"published": sum(1 for f in files.values() if f["kind"] == k), "excluded": sum(1 for v in excluded.values() if v["kind"] == k),
                   "quarantined": sum(1 for v in quarantined.values() if v["kind"] == k)} for k in KINDS}
    n_ok = sum(1 for f in files.values() if f["status"] == "OK")
    manifest = {
        "contract": CONTRACT_V4, "dataset": "EOD2", "datasetId": res.dataset_id, "datasetHash": dataset_hash_v4(files, tails, cal_sha, pol),
        "policyHash": pol, "histEnd": hist_end, "snapshotBase": "", "createdAt": er.utc_now_iso_ist(now), "exporterVersion": exporter_version,
        "latestSession": latest, "latestCompletedSession": latest, "windowStart": reg.default_start, "eqHistoryFloor": reg.eq_floor,
        "publishStatus": "PUBLISHABLE", "certification": "PASS" if not (excluded or quarantined) else "PARTIAL",
        "providerMixing": False, "fallbackUsed": False, "holidays": cal["holidays"],
        "calendar": {"id": cal["id"], "path": cal_rel, "sha256": cal_sha, "bytes": len(cal_bytes), "cutoffIST": cal["cutoffIST"],
                     "coverageFrom": cal["coverageFrom"], "coverageTo": cal["coverageTo"], "verifiedThrough": cal["verifiedThrough"]},
        "adjustmentPolicy": ADJUSTMENT_V4, "tails": tails, "files": files, "excluded": excluded, "quarantined": quarantined,
        "pendingActions": pending, "ref": ref,
        "summary": {"requested": len(items), "ok": n_ok, "warn": len(files) - n_ok, "excluded": len(excluded), "quarantined": len(quarantined),
                    "byKind": by_kind, "historyTruncated": len(pending), "bytes": total},
    }
    problems = verify_v4(out_dir, manifest)
    if not files:
        problems.append("zero instruments usable")
    if problems:
        manifest["publishStatus"] = "UNPUBLISHABLE"
        res.message = "; ".join(problems[:10])
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False) + "\n")
    res.manifest, res.publish_status, res.pending, res.total_bytes = manifest, manifest["publishStatus"], pending, total
    return res


def propose_actions(eod2_dir, config_path):
    """Every unapproved overnight gap beyond the price band, per stock: [(symbol, date, gap, prevClose, open, suggestedFactor, isLast)]."""
    reg = Registry.load(config_path)
    src = er.Eod2Source(eod2_dir)
    out = []
    for it in reg.stocks:
        fn, rs = ident.resolve_etf(it, src)
        if fn is None:
            continue
        rows, findings = src.read_bars(fn)
        p = V.prepare("EQ", it, rows, findings, reg)
        if p.history_truncated:
            ev = p.history_truncated["events"]
            for e in ev:
                out.append((it["symbol"], e["date"], e["gap"], e["prevClose"], e["open"], e["suggestedFactor"], e is ev[-1]))
    return out


def format_summary(res):
    if res.manifest is None:
        return "v4 export failed: " + res.message
    m, s = res.manifest, res.manifest["summary"]
    lines = ["v4 %s  %s  %s  latest %s  histEnd %s" % (m["datasetId"], m["publishStatus"], m["certification"], m["latestCompletedSession"], m["histEnd"])]
    for k in KINDS:
        b = s["byKind"][k]
        lines.append("  %-5s published %d, excluded %d, quarantined %d" % (k, b["published"], b["excluded"], b["quarantined"]))
    lines.append("  ok %d, warn %d; stocks with a truncated history %d; %.1f MB" % (s["ok"], s["warn"], s["historyTruncated"], s["bytes"] / 1e6))
    if res.message:
        lines.append("  problems: " + res.message)
    return "\n".join(lines)
