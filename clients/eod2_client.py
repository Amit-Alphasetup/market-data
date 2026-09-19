"""EOD2 data client v3 for Python (contract "alphadesk-eod2/3") — same API and same results as clients/eod2-client.js.

    client = EOD2Client(base_url, cache="memory" | "<folder>" | None, fetch=None)     # cache default: ~/.eod2_v3/
    snap = client.open_snapshot()            # dict {"ok": False, "code", "detail"} on any data problem, otherwise a Snapshot
    snap.id, snap.manifest, snap.calendar    # calendar sha256 is checked against manifest.calendar.sha256
    snap.get_series(sym, force=False)        # {"ok": True, "bars", "meta"} | {"ok": False, "code", "detail"}
    snap.check_coverage(sym, from_=..., to=..., fields=[...], warmup_sessions=0, mode="certified", exec_dates=[])   # series must be loaded
    snap.verify_all(force=False, on_progress=None)
    client.clear_cache(dataset_id=None, symbol=None)

Rules: a snapshot never reloads the manifest and reads only under its own snapshotBase; cache key eod2v3:<datasetId>:<symbol>:<sha256>; a cache
hit must re-hash to the manifest sha256; force bypasses the cache and re-hashes the raw bytes; every series is re-validated against section 4.5;
data problems never raise; returned bars are tuples of tuples (immutable). Only the standard library is used.
"""
import hashlib
import json
import math
import os
import urllib.request
from datetime import date as _date

CONTRACT = "alphadesk-eod2/3"
VERSION = "3.0.0"
FIELDS = ["date", "open", "high", "low", "close", "volume"]
MIGRATED = "migrated-v2-2026-09-11"
OHLC_WARN = 0.0005
FIELD_INDEX = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
ELIGIBLE_WARN = ("ISIN_MISSING", "SMALL_OHLC_DISCREPANCY")
INFO_CODES = ("NOT_YET_LISTED", "DELISTED_BEFORE_WINDOW")


def _fail(code, detail=""):
    return {"ok": False, "code": code, "detail": "" if detail is None else str(detail)}


def _is_date(v):
    if not isinstance(v, str) or len(v) != 10 or v[4] != "-" or v[7] != "-":
        return False
    try:
        return _date.fromisoformat(v).isoformat() == v and v.replace("-", "").isdigit()
    except ValueError:
        return False


def _reject_constant(x):
    raise ValueError("non-standard JSON constant " + x)


def _loads(b):
    """json.loads that rejects NaN / Infinity literals exactly like JavaScript's JSON.parse."""
    return json.loads(b.decode("utf-8"), parse_constant=_reject_constant)


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


def _norm(s):
    return str(s).replace("_", " ").strip().upper()


def _lower_bound(arr, key, sel=lambda x: x):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if sel(arr[mid]) < key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ohlc_violation(r):
    o, h, l, c = r[1], r[2], r[3], r[4]
    worst = 0.0
    present = [x for x in (o, c, l) if x is not None]
    if h is not None and present and h > 0:
        worst = max(worst, (max(present) - h) / h)
    present = [x for x in (o, c, h) if x is not None]
    if l is not None and present and l > 0:
        worst = max(worst, (l - min(present)) / l)
    return max(0.0, worst)


def validate_series(doc, entry):
    """-> {"ok": True, "smallOhlc": n} or a failure dict (section 4.5)."""
    if not isinstance(doc, dict):
        return _fail("SCHEMA_INVALID", "series is not an object")
    if doc.get("contract") != CONTRACT or doc.get("provider") != "EOD2":
        return _fail("SCHEMA_INVALID", "contract/provider")
    if doc.get("kind") != entry.get("kind") or doc.get("id") != entry.get("id"):
        return _fail("WRONG_EXCHANGE_OR_KIND", "series says %s %s, manifest says %s %s" % (doc.get("kind"), doc.get("id"), entry.get("kind"), entry.get("id")))
    if doc.get("fields") != FIELDS:
        return _fail("SCHEMA_INVALID", "fields")
    if not isinstance(doc.get("adjustment"), str) or not doc["adjustment"]:
        return _fail("ADJUSTMENT_POLICY_UNKNOWN", "adjustment")
    bars = doc.get("bars")
    if not isinstance(bars, list) or not bars:
        return _fail("SCHEMA_INVALID", "no bars")
    etf = entry.get("kind") == "ETF"
    warn, prev = 0, None
    for i, r in enumerate(bars):
        if not isinstance(r, list) or len(r) != 6:
            return _fail("SCHEMA_INVALID", "row %d is not a 6-tuple" % i)
        if not _is_date(r[0]):
            return _fail("INVALID_DATE", str(r[0]))
        if prev is not None and not r[0] > prev:
            return _fail("SCHEMA_INVALID", "dates not strictly ascending at " + r[0])
        prev = r[0]
        for j in range(1, 6):
            v = r[j]
            if v is None:
                if etf and j <= 4:
                    return _fail("MISSING_REQUIRED_FIELD", "%s on %s" % (FIELDS[j], r[0]))
                continue
            if not _num(v):
                return _fail("NONFINITE_VALUE", "%s on %s" % (FIELDS[j], r[0]))
            if j <= 4 and not v > 0:
                return _fail("SCHEMA_INVALID", "non-positive %s on %s" % (FIELDS[j], r[0]))
            if j == 5 and (v < 0 or math.floor(v) != v):
                return _fail("SCHEMA_INVALID", "volume must be an integer >= 0 on " + r[0])
        viol = _ohlc_violation(r)
        if viol > OHLC_WARN:
            return _fail("OHLC_INCONSISTENT", r[0])
        if viol > 0:
            warn += 1
    return {"ok": True, "smallOhlc": warn}


class _MemoryCache:
    kind = "memory"

    def __init__(self):
        self.m = {}

    def get(self, k):
        return self.m.get(k)

    def put(self, k, b):
        self.m[k] = bytes(b)

    def delete(self, pred):
        ks = [k for k in self.m if pred(k)]
        for k in ks:
            del self.m[k]
        return len(ks)

    def keys(self):
        return list(self.m)


class _DirCache:
    kind = "dir"

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def _p(self, k):
        return os.path.join(self.folder, k.replace(":", "__").replace("/", "_") + ".bin")

    def get(self, k):
        try:
            with open(self._p(k), "rb") as f:
                return f.read()
        except OSError:
            return None

    def put(self, k, b):
        with open(self._p(k), "wb") as f:
            f.write(bytes(b))

    def keys(self):
        return [n[:-4].replace("__", ":") for n in os.listdir(self.folder) if n.endswith(".bin")]

    def delete(self, pred):
        n = 0
        for k in self.keys():
            if pred(k):
                os.remove(self._p(k))
                n += 1
        return n


def _default_fetch(url, no_store=False):
    if url.startswith("file:") or (len(url) > 2 and url[1] == ":" and url[2] in "\\/") or url.startswith("/"):
        path = url[7:] if url.startswith("file://") else url
        with open(path, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "eod2-client", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


class EOD2Client:
    def __init__(self, base_url, cache=None, fetch=None):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._fetch = fetch or _default_fetch
        if cache == "memory":
            self._cache = _MemoryCache()
        else:
            self._cache = _DirCache(cache or os.path.join(os.path.expanduser("~"), ".eod2_v3"))
        self.cache_kind = self._cache.kind

    def _get_bytes(self, url):
        try:
            b = self._fetch(url)
        except Exception as e:                       # data problems never raise
            return None, _fail("SOURCE_UNAVAILABLE", "%s: %s" % (type(e).__name__, e))
        if b is None:
            return None, _fail("SOURCE_UNAVAILABLE", "HTTP 404 " + url)
        return bytes(b), None

    def open_snapshot(self):
        b, err = self._get_bytes(self.base_url + "data/manifest.json")
        if err:
            return err
        try:
            m = _loads(b)
        except (ValueError, UnicodeDecodeError):
            return _fail("SCHEMA_INVALID", "manifest is not JSON")
        if not isinstance(m, dict) or m.get("contract") != CONTRACT:
            return _fail("SCHEMA_INVALID", "manifest contract")
        if m.get("publishStatus") != "PUBLISHABLE":
            return _fail("SOURCE_UNAVAILABLE", "snapshot is %s" % m.get("publishStatus"))
        cal_meta = m.get("calendar")
        if (not isinstance(m.get("datasetId"), str) or not isinstance(m.get("snapshotBase"), str) or not isinstance(m.get("files"), dict)
                or not isinstance(cal_meta, dict) or not isinstance(cal_meta.get("path"), str)
                or not (isinstance(cal_meta.get("sha256"), str) and len(cal_meta["sha256"]) == 64 and all(c in "0123456789abcdef" for c in cal_meta["sha256"]))):
            return _fail("SCHEMA_INVALID", "manifest fields")
        sbase = self.base_url + "data/" + m["snapshotBase"]
        cb, err = self._get_bytes(sbase + cal_meta["path"])
        if err:
            return err
        if _sha256(cb) != cal_meta["sha256"]:
            return _fail("CALENDAR_UNVERIFIED", "calendar sha256 does not match the manifest")
        try:
            cal = _loads(cb)
        except (ValueError, UnicodeDecodeError):
            return _fail("CALENDAR_UNVERIFIED", "calendar is not JSON")
        if not isinstance(cal, dict) or not isinstance(cal.get("sessions"), list) or not cal["sessions"] or not _is_date(cal.get("verifiedThrough")):
            return _fail("CALENDAR_UNVERIFIED", "calendar content")
        return Snapshot(self, m, cal, sbase)

    def clear_cache(self, dataset_id=None, symbol=None):
        def pred(k):
            p = str(k).split(":")
            return str(k).startswith("eod2v3:") and (not dataset_id or p[1] == dataset_id) and (not symbol or p[2] == symbol)
        return self._cache.delete(pred)

    # test-only hooks (used by the conformance runner)
    def _test_corrupt_cache(self, symbol):
        n = 0
        for k in self._cache.keys():
            if str(k).split(":")[2] == symbol:
                b = bytearray(self._cache.get(k))
                b[len(b) >> 1] = (b[len(b) >> 1] + 1) & 255
                self._cache.put(k, bytes(b))
                n += 1
        return n

    def _test_cache_keys(self):
        return sorted(self._cache.keys())


class Snapshot:
    ok = True

    def __init__(self, client, manifest, calendar, sbase):
        self._c, self.manifest, self.calendar, self._sbase = client, manifest, calendar, sbase
        self.id = manifest["datasetId"]
        self._loaded = {}
        self._sessions = sorted(calendar["sessions"])

    def _entry_for(self, sym):
        k = _norm(sym)
        for n in self.manifest["files"]:
            if _norm(n) == k:
                return n, self.manifest["files"][n], None
        for n, x in (self.manifest.get("quarantined") or {}).items():
            if _norm(n) == k:
                return None, None, _fail(x.get("code") or "EXPORT_FAILED", "quarantined: " + json.dumps(x.get("events") or [], separators=(",", ":")))
        for n, x in (self.manifest.get("excluded") or {}).items():
            if _norm(n) == k:
                return None, None, _fail(x.get("code") or "EXPORT_FAILED", x.get("detail") or "")
        return None, None, _fail("NOT_IN_EXPORT", str(sym))

    def get_series(self, sym, force=False):
        key, entry, blocked = self._entry_for(sym)
        if blocked:
            return blocked
        if not force and key in self._loaded:
            return self._loaded[key]["result"]
        m = self.manifest
        ckey = "eod2v3:%s:%s:%s" % (m["datasetId"], key, entry["sha256"])
        data, from_cache, status = None, False, "MISS"
        if not force:
            cached = self._c._cache.get(ckey)
            if cached is not None:
                if _sha256(cached) == entry["sha256"]:
                    data, from_cache, status = cached, True, "HIT"
                else:
                    self._c._cache.delete(lambda k: k == ckey)
                    status = "REPAIRED"
        else:
            status = "BYPASSED"
        if data is None:
            data, err = self._c._get_bytes(self._sbase + entry["path"])
            if err:
                return err
            if _sha256(data) != entry["sha256"]:
                return _fail("HASH_MISMATCH", "%s: file bytes do not match the manifest sha256" % key)
        try:
            doc = _loads(data)
        except (ValueError, UnicodeDecodeError):
            return _fail("SCHEMA_INVALID", "%s is not JSON" % key)
        v = validate_series(doc, entry)
        if not v["ok"]:
            return v
        if not from_cache:
            self._c._cache.put(ckey, data)
        bars = tuple(tuple(r) for r in doc["bars"])
        meta = {"symbol": key, "id": entry["id"], "kind": entry["kind"], "sha256": entry["sha256"], "first": bars[0][0], "last": bars[-1][0], "bars": len(bars),
                "status": entry.get("status"), "reasons": tuple(dict(r) for r in entry.get("reasons", [])), "noTradeDays": tuple(entry.get("noTradeDays") or ()),
                "smallOhlcDiscrepancies": v["smallOhlc"], "fromCache": from_cache, "cacheStatus": status}
        result = {"ok": True, "bars": bars, "meta": meta}
        self._loaded[key] = {"result": result, "entry": entry}
        return result

    def check_coverage(self, sym, from_=None, to=None, fields=None, warmup_sessions=0, mode="certified", exec_dates=None):
        fields = fields if fields is not None else ["close"]
        warm = 0 if warmup_sessions is None else warmup_sessions
        exec_dates = exec_dates or []

        def out(code, **extra):
            d = {"ok": code == "OK", "code": code, "reasons": [], "missing": [], "firstTradable": None}
            d.update(extra)
            return d
        if not _is_date(from_) or not _is_date(to) or from_ > to:
            return out("SCHEMA_INVALID", detail="from/to must be real dates with from <= to")
        key, entry, blocked = self._entry_for(sym)
        if blocked:
            return out(blocked["code"], detail=blocked["detail"])
        L = self._loaded.get(key)
        if not L:
            return out("SOURCE_UNAVAILABLE", detail="series not loaded: await getSeries first")
        bars = L["result"]["bars"]
        first = bars[0][0]
        cal, m, sessions = self.calendar, self.manifest, self._sessions
        if from_ > cal["verifiedThrough"] or to > cal["verifiedThrough"]:
            return out("CALENDAR_UNVERIFIED", detail="beyond verifiedThrough " + cal["verifiedThrough"])
        latest = m["latestCompletedSession"]
        if to > latest:
            return out("STALE_DATASET", detail="snapshot ends " + latest)
        rep = [r["code"] for r in entry.get("reasons", [])]
        listing = entry.get("listingDate") or None
        first_obs = entry.get("firstObservedDate") or first
        range_start, infos = from_, []
        if listing:
            if listing > from_:
                range_start = listing
                infos.append("NOT_YET_LISTED")
        elif first_obs > from_:
            range_start = first_obs
        reasons = sorted(set([c for c in rep if c != "OK"] + infos))

        def res(code, **extra):
            return out(code, reasons=reasons, **extra)
        if mode == "certified":
            for r in entry.get("reasons", []):
                rc = r["code"]
                if rc in ELIGIBLE_WARN or rc in INFO_CODES or rc == "OK":
                    continue
                if rc == "LISTING_DATE_UNVERIFIED":
                    if from_ < first_obs:
                        return res("LISTING_DATE_UNVERIFIED", detail="certified window starts before the first observed bar " + first_obs)
                    continue
                if rc == "APPROVAL_EVIDENCE_MISSING":
                    if r.get("provenance") != MIGRATED:
                        return res("APPROVAL_EVIDENCE_MISSING", detail="approval without evidence is not migrated-v2")
                    continue
                return res(rc, detail="warning not eligible for a certified run")
        have = {b[0] for b in bars}
        no_trade = set(entry.get("noTradeDays") or [])
        end_d = to if to < latest else latest
        missing = []
        for k in range(_lower_bound(sessions, range_start), len(sessions)):
            d = sessions[k]
            if d > end_d:
                break
            if d not in have and d not in no_trade:
                missing.append(d)
        if missing:
            return res("MISSING_EXPECTED_SESSION", missing=missing[:10], detail="%d session(s) missing" % len(missing))
        need = [f for f in fields if f in FIELD_INDEX]
        for row in bars:
            if row[0] < range_start or row[0] > end_d:
                continue
            for f in need:
                if row[FIELD_INDEX[f]] is None:
                    return res("MISSING_REQUIRED_FIELD", detail="%s is null on %s" % (f, row[0]))
        for d in exec_dates:
            idx = _lower_bound(bars, d, lambda r: r[0])
            if idx >= len(bars) or bars[idx][0] != d or bars[idx][1] is None:
                return res("MISSING_EXECUTION_OPEN", detail="no OPEN on " + d)
        first_tradable = None
        for q in range(_lower_bound(sessions, range_start), len(sessions)):
            dq = sessions[q]
            if dq > end_d:
                break
            if _lower_bound(bars, dq, lambda r: r[0]) >= warm:
                first_tradable = dq
                break
        if first_tradable is None:
            return res("INSUFFICIENT_WARMUP", detail="fewer than %d bars precede any session in the window" % warm)
        return res("OK", firstTradable=first_tradable)

    def verify_all(self, force=False, on_progress=None):
        keys = list(self.manifest["files"])
        failures = []
        for i, k in enumerate(keys):
            r = self.get_series(k, force=force)
            if not r["ok"]:
                failures.append({"symbol": k, "code": r["code"], "detail": r["detail"]})
            if on_progress:
                on_progress(i + 1, len(keys))
        return {"ok": not failures, "checked": len(keys), "failures": failures}
