/* EOD2 data client v3 — classic script (no `export`), defines globalThis.EOD2Client. Vendored into AlphaDesk (A2-5). Contract "alphadesk-eod2/3".
 *
 *   const client = EOD2Client.create({ baseUrl, cache: 'memory' | 'indexeddb', dbName: 'eod2_v3', fetch });
 *   const snap = await client.openSnapshot();          // {ok:false, code, detail} on any data problem, otherwise a frozen handle
 *   snap.id; snap.manifest; snap.calendar;             // calendar sha256 checked against manifest.calendar.sha256
 *   await snap.getSeries(sym [, {force}])              // {ok:true, bars, meta} | {ok:false, code, detail}
 *   snap.checkCoverage(sym, {from, to, fields, warmupSessions, mode, execDates})   // synchronous; series must have been loaded
 *   await snap.verifyAll({force, onProgress})
 *   client.clearCache({datasetId, symbol})
 *
 * Rules: a handle never reloads the manifest and fetches only under its own snapshotBase; cache key eod2v3:<datasetId>:<symbol>:<sha256>,
 * a cache hit must re-hash to the manifest sha256; `force` bypasses the cache and re-hashes the raw network bytes; every series is re-validated
 * against section 4.5; data problems never throw; returned bars (array and every row) are frozen.
 */
(function (root) {
  'use strict';
  var CONTRACT = 'alphadesk-eod2/3';
  var VERSION = '3.0.0';
  var FIELDS = ['date', 'open', 'high', 'low', 'close', 'volume'];
  var MIGRATED = 'migrated-v2-2026-09-11';
  var OHLC_WARN = 0.0005;
  var ETF_REQUIRED = ['open', 'high', 'low', 'close'];
  var FIELD_INDEX = { open: 1, high: 2, low: 3, close: 4, volume: 5 };
  var ELIGIBLE_WARN = { ISIN_MISSING: 1, SMALL_OHLC_DISCREPANCY: 1 };
  var INFO_CODES = { NOT_YET_LISTED: 1, DELISTED_BEFORE_WINDOW: 1 };

  function fail(code, detail) { return { ok: false, code: code, detail: detail == null ? '' : String(detail) }; }
  function isDate(v) {
    if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
    var d = new Date(v + 'T00:00:00Z');
    return !isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v;
  }
  function hex(buf) { var a = new Uint8Array(buf), s = ''; for (var i = 0; i < a.length; i++) s += ('0' + a[i].toString(16)).slice(-2); return s; }
  async function sha256(bytes) { return hex(await root.crypto.subtle.digest('SHA-256', bytes)); }
  function decode(bytes) { return new TextDecoder('utf-8').decode(bytes); }
  function lowerBound(arr, key, sel) { var lo = 0, hi = arr.length; while (lo < hi) { var m = (lo + hi) >> 1; if (sel(arr[m]) < key) lo = m + 1; else hi = m; } return lo; }
  function normKey(s) { return String(s).replace(/_/g, ' ').trim().toUpperCase(); }

  /* ---------- caches ---------- */
  function memoryCache() {
    var m = new Map();
    return {
      kind: 'memory',
      get: async function (k) { return m.has(k) ? m.get(k) : null; },
      put: async function (k, bytes) { m.set(k, bytes); },
      del: async function (pred) { var n = 0; Array.from(m.keys()).forEach(function (k) { if (pred(k)) { m.delete(k); n++; } }); return n; },
      keys: async function () { return Array.from(m.keys()); }
    };
  }
  function idbCache(dbName) {
    var dbp = null;
    function db() {
      if (dbp) return dbp;
      dbp = new Promise(function (res, rej) {
        var rq = root.indexedDB.open(dbName, 1);
        rq.onupgradeneeded = function () { if (!rq.result.objectStoreNames.contains('series')) rq.result.createObjectStore('series'); };
        rq.onsuccess = function () { res(rq.result); };
        rq.onerror = function () { rej(rq.error); };
      });
      return dbp;
    }
    function tx(mode, fn) { return db().then(function (d) { return new Promise(function (res, rej) { var r = fn(d.transaction('series', mode).objectStore('series')); r.onsuccess = function () { res(r.result); }; r.onerror = function () { rej(r.error); }; }); }); }
    return {
      kind: 'indexeddb',
      get: function (k) { return tx('readonly', function (s) { return s.get(k); }).then(function (v) { return v ? new Uint8Array(v) : null; }); },
      put: function (k, bytes) { return tx('readwrite', function (s) { return s.put(bytes.slice().buffer, k); }); },
      keys: function () { return tx('readonly', function (s) { return s.getAllKeys(); }); },
      del: async function (pred) { var ks = await this.keys(), n = 0; for (var i = 0; i < ks.length; i++) if (pred(ks[i])) { await tx('readwrite', function (s) { return s.delete(ks[i]); }); n++; } return n; }
    };
  }

  /* ---------- section 4.5 validation ---------- */
  function ohlcViolation(r) {
    var o = r[1], h = r[2], l = r[3], c = r[4], worst = 0, present;
    present = [o, c, l].filter(function (x) { return x !== null; });
    if (h !== null && present.length && h > 0) worst = Math.max(worst, (Math.max.apply(null, present) - h) / h);
    present = [o, c, h].filter(function (x) { return x !== null; });
    if (l !== null && present.length && l > 0) worst = Math.max(worst, (l - Math.min.apply(null, present)) / l);
    return Math.max(0, worst);
  }
  function validateSeries(doc, entry) {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return fail('SCHEMA_INVALID', 'series is not an object');
    if (doc.contract !== CONTRACT || doc.provider !== 'EOD2') return fail('SCHEMA_INVALID', 'contract/provider');
    if (doc.kind !== entry.kind || doc.id !== entry.id) return fail('WRONG_EXCHANGE_OR_KIND', 'series says ' + doc.kind + ' ' + doc.id + ', manifest says ' + entry.kind + ' ' + entry.id);
    if (!Array.isArray(doc.fields) || doc.fields.join(',') !== FIELDS.join(',')) return fail('SCHEMA_INVALID', 'fields');
    if (typeof doc.adjustment !== 'string' || !doc.adjustment) return fail('ADJUSTMENT_POLICY_UNKNOWN', 'adjustment');
    if (!Array.isArray(doc.bars) || !doc.bars.length) return fail('SCHEMA_INVALID', 'no bars');
    var etf = entry.kind === 'ETF', warn = 0, prev = null, i, j, r;
    for (i = 0; i < doc.bars.length; i++) {
      r = doc.bars[i];
      if (!Array.isArray(r) || r.length !== 6) return fail('SCHEMA_INVALID', 'row ' + i + ' is not a 6-tuple');
      if (!isDate(r[0])) return fail('INVALID_DATE', String(r[0]));
      if (prev !== null && !(r[0] > prev)) return fail('SCHEMA_INVALID', 'dates not strictly ascending at ' + r[0]);
      prev = r[0];
      for (j = 1; j <= 5; j++) {
        var v = r[j];
        if (v === null) { if (etf && j <= 4) return fail('MISSING_REQUIRED_FIELD', FIELDS[j] + ' on ' + r[0]); continue; }
        if (typeof v !== 'number' || !isFinite(v)) return fail('NONFINITE_VALUE', FIELDS[j] + ' on ' + r[0]);
        if (j <= 4 && !(v > 0)) return fail('SCHEMA_INVALID', 'non-positive ' + FIELDS[j] + ' on ' + r[0]);
        if (j === 5 && (v < 0 || Math.floor(v) !== v)) return fail('SCHEMA_INVALID', 'volume must be an integer >= 0 on ' + r[0]);
      }
      var viol = ohlcViolation(r);
      if (viol > OHLC_WARN) return fail('OHLC_INCONSISTENT', r[0]);
      if (viol > 0) warn++;
    }
    return { ok: true, smallOhlc: warn };
  }

  function freezeRows(bars) { for (var i = 0; i < bars.length; i++) Object.freeze(bars[i]); return Object.freeze(bars); }

  /* ---------- client ---------- */
  function create(opts) {
    opts = opts || {};
    var baseUrl = String(opts.baseUrl || './');
    if (baseUrl.slice(-1) !== '/') baseUrl += '/';
    var doFetch = opts.fetch || (root.fetch && root.fetch.bind(root));
    var cache = opts.cache === 'indexeddb' && root.indexedDB ? idbCache(opts.dbName || 'eod2_v3') : (opts.cache && typeof opts.cache === 'object' ? opts.cache : memoryCache());

    async function getBytes(url, noStore) {
      try {
        var r = await doFetch(url, noStore ? { cache: 'no-store' } : undefined);
        if (!r || !r.ok) return { err: fail('SOURCE_UNAVAILABLE', 'HTTP ' + (r ? r.status : '?') + ' ' + url) };
        return { bytes: new Uint8Array(await r.arrayBuffer()) };
      } catch (e) { return { err: fail('SOURCE_UNAVAILABLE', (e && e.message) || e) }; }
    }

    async function openSnapshot() {
      var got = await getBytes(baseUrl + 'data/manifest.json', true);
      if (got.err) return got.err;
      var m;
      try { m = JSON.parse(decode(got.bytes)); } catch (e) { return fail('SCHEMA_INVALID', 'manifest is not JSON'); }
      if (!m || m.contract !== CONTRACT) return fail('SCHEMA_INVALID', 'manifest contract');
      if (m.publishStatus !== 'PUBLISHABLE') return fail('SOURCE_UNAVAILABLE', 'snapshot is ' + m.publishStatus);
      if (typeof m.datasetId !== 'string' || typeof m.snapshotBase !== 'string' || !m.files || typeof m.files !== 'object' || !m.calendar || typeof m.calendar.path !== 'string' || !/^[0-9a-f]{64}$/.test(m.calendar.sha256 || '')) return fail('SCHEMA_INVALID', 'manifest fields');
      var sbase = baseUrl + 'data/' + m.snapshotBase;
      var cb = await getBytes(sbase + m.calendar.path, true);
      if (cb.err) return cb.err;
      if ((await sha256(cb.bytes)) !== m.calendar.sha256) return fail('CALENDAR_UNVERIFIED', 'calendar sha256 does not match the manifest');
      var cal;
      try { cal = JSON.parse(decode(cb.bytes)); } catch (e) { return fail('CALENDAR_UNVERIFIED', 'calendar is not JSON'); }
      if (!cal || !Array.isArray(cal.sessions) || !cal.sessions.length || !isDate(cal.verifiedThrough)) return fail('CALENDAR_UNVERIFIED', 'calendar content');
      return buildHandle(m, cal, sbase);
    }

    function buildHandle(m, cal, sbase) {
      var loaded = {};                      // symbol -> {rows: [...], entry}
      var sessions = cal.sessions.slice().sort();
      var sessionSet = new Set(sessions);
      function entryFor(sym) {
        var k = normKey(sym), found = null;
        Object.keys(m.files).forEach(function (n) { if (normKey(n) === k) found = n; });
        if (found) return { key: found, entry: m.files[found] };
        var q = null, x = null;
        Object.keys(m.quarantined || {}).forEach(function (n) { if (normKey(n) === k) q = n; });
        Object.keys(m.excluded || {}).forEach(function (n) { if (normKey(n) === k) x = n; });
        if (q) return { blocked: fail(m.quarantined[q].code || 'EXPORT_FAILED', 'quarantined: ' + JSON.stringify(m.quarantined[q].events || [])) };
        if (x) return { blocked: fail(m.excluded[x].code || 'EXPORT_FAILED', m.excluded[x].detail || '') };
        return { blocked: fail('NOT_IN_EXPORT', String(sym)) };
      }
      async function getSeries(sym, o) {
        var force = !!(o && o.force);
        var e = entryFor(sym);
        if (e.blocked) return e.blocked;
        var key = e.key, entry = e.entry;
        if (!force && loaded[key] && loaded[key].result) return loaded[key].result;
        var ckey = 'eod2v3:' + m.datasetId + ':' + key + ':' + entry.sha256;
        var bytes = null, fromCache = false, cacheStatus = 'MISS';
        if (!force) {
          var cached = await cache.get(ckey);
          if (cached) {
            if ((await sha256(cached)) === entry.sha256) { bytes = cached; fromCache = true; cacheStatus = 'HIT'; }
            else { await cache.del(function (k) { return k === ckey; }); cacheStatus = 'REPAIRED'; }
          }
        } else cacheStatus = 'BYPASSED';
        if (!bytes) {
          var g = await getBytes(sbase + entry.path, true);
          if (g.err) return g.err;
          if ((await sha256(g.bytes)) !== entry.sha256) return fail('HASH_MISMATCH', key + ': file bytes do not match the manifest sha256');
          bytes = g.bytes;
        }
        var doc;
        try { doc = JSON.parse(decode(bytes)); } catch (err) { return fail('SCHEMA_INVALID', key + ' is not JSON'); }
        var v = validateSeries(doc, entry);
        if (!v.ok) return v;
        if (!fromCache) await cache.put(ckey, bytes);
        var reasons = (entry.reasons || []).map(function (r) { return Object.freeze(Object.assign({}, r)); });
        var result = { ok: true, bars: freezeRows(doc.bars), meta: Object.freeze({ symbol: key, id: entry.id, kind: entry.kind, sha256: entry.sha256, first: doc.bars[0][0], last: doc.bars[doc.bars.length - 1][0], bars: doc.bars.length, status: entry.status, reasons: Object.freeze(reasons), noTradeDays: Object.freeze((entry.noTradeDays || []).slice()), smallOhlcDiscrepancies: v.smallOhlc, fromCache: fromCache, cacheStatus: cacheStatus }) };
        Object.freeze(result);
        loaded[key] = { result: result, entry: entry };
        return result;
      }

      function checkCoverage(sym, o) {
        o = o || {};
        var mode = o.mode || 'certified', fields = o.fields || ['close'], warm = o.warmupSessions == null ? 0 : o.warmupSessions;
        var from = o.from, to = o.to, execDates = o.execDates || [];
        var out = function (code, extra) { return Object.assign({ ok: code === 'OK', code: code, reasons: [], missing: [], firstTradable: null }, extra || {}); };
        if (!isDate(from) || !isDate(to) || from > to) return out('SCHEMA_INVALID', { detail: 'from/to must be real dates with from <= to' });
        var e = entryFor(sym);
        if (e.blocked) return out(e.blocked.code, { detail: e.blocked.detail });
        var L = loaded[e.key];
        if (!L || !L.result) return out('SOURCE_UNAVAILABLE', { detail: 'series not loaded: await getSeries first' });
        var entry = e.entry, bars = L.result.bars, first = bars[0][0];
        if (from > cal.verifiedThrough || to > cal.verifiedThrough) return out('CALENDAR_UNVERIFIED', { detail: 'beyond verifiedThrough ' + cal.verifiedThrough });
        var latest = m.latestCompletedSession;
        if (to > latest) return out('STALE_DATASET', { detail: 'snapshot ends ' + latest });
        var repReasons = (entry.reasons || []).map(function (r) { return r.code; });
        var listing = entry.listingDate || null, firstObs = entry.firstObservedDate || first;
        var unverified = repReasons.indexOf('LISTING_DATE_UNVERIFIED') >= 0;
        var rangeStart = from;
        var infos = [];
        if (listing) { if (listing > from) { rangeStart = listing; infos.push('NOT_YET_LISTED'); } }
        else if (firstObs > from) rangeStart = firstObs;
        var uniq = function (a) { return a.filter(function (x, i) { return a.indexOf(x) === i; }).sort(); };
        var reasons = uniq(repReasons.filter(function (c) { return c !== 'OK'; }).concat(infos));
        var res = function (code, extra) { return out(code, Object.assign({ reasons: reasons }, extra || {})); };
        if (mode === 'certified') {
          for (var i = 0; i < entry.reasons.length; i++) {
            var rc = entry.reasons[i].code;
            if (ELIGIBLE_WARN[rc] || INFO_CODES[rc] || rc === 'OK') continue;
            if (rc === 'LISTING_DATE_UNVERIFIED') { if (from < firstObs) return res('LISTING_DATE_UNVERIFIED', { detail: 'certified window starts before the first observed bar ' + firstObs }); continue; }
            if (rc === 'APPROVAL_EVIDENCE_MISSING') { if (entry.reasons[i].provenance !== MIGRATED) return res('APPROVAL_EVIDENCE_MISSING', { detail: 'approval without evidence is not migrated-v2' }); continue; }
            return res(rc, { detail: 'warning not eligible for a certified run' });
          }
        }
        var have = new Set(bars.map(function (b) { return b[0]; })), noTrade = new Set(entry.noTradeDays || []);
        var endD = to < latest ? to : latest, missing = [];
        var a = lowerBound(sessions, rangeStart, function (x) { return x; });
        for (var k = a; k < sessions.length && sessions[k] <= endD; k++) { var d = sessions[k]; if (!have.has(d) && !noTrade.has(d)) missing.push(d); }
        if (missing.length) return res('MISSING_EXPECTED_SESSION', { missing: missing.slice(0, 10), detail: missing.length + ' session(s) missing' });
        var need = fields.filter(function (f) { return FIELD_INDEX[f]; });
        for (var b = 0; b < bars.length; b++) {
          var row = bars[b];
          if (row[0] < rangeStart || row[0] > endD) continue;
          for (var f = 0; f < need.length; f++) if (row[FIELD_INDEX[need[f]]] === null) return res('MISSING_REQUIRED_FIELD', { detail: need[f] + ' is null on ' + row[0] });
        }
        for (var x = 0; x < execDates.length; x++) {
          var idx = lowerBound(bars, execDates[x], function (r) { return r[0]; });
          var rowx = bars[idx];
          if (!rowx || rowx[0] !== execDates[x] || rowx[1] === null) return res('MISSING_EXECUTION_OPEN', { detail: 'no OPEN on ' + execDates[x] });
        }
        var firstTradable = null;
        var s0 = lowerBound(sessions, rangeStart, function (v) { return v; });
        for (var q = s0; q < sessions.length && sessions[q] <= endD; q++) {
          var dq = sessions[q];
          if (lowerBound(bars, dq, function (r) { return r[0]; }) >= warm) { firstTradable = dq; break; }
        }
        if (firstTradable === null) return res('INSUFFICIENT_WARMUP', { detail: 'fewer than ' + warm + ' bars precede any session in the window' });
        return res('OK', { firstTradable: firstTradable });
      }

      async function verifyAll(o) {
        o = o || {};
        var keys = Object.keys(m.files), failures = [];
        for (var i = 0; i < keys.length; i++) {
          var r = await getSeries(keys[i], { force: !!o.force });
          if (!r.ok) failures.push({ symbol: keys[i], code: r.code, detail: r.detail });
          if (o.onProgress) o.onProgress(i + 1, keys.length);
        }
        return { ok: failures.length === 0, checked: keys.length, failures: failures };
      }

      return Object.freeze({ ok: true, id: m.datasetId, manifest: m, calendar: cal, getSeries: getSeries, checkCoverage: checkCoverage, verifyAll: verifyAll });
    }

    async function clearCache(o) {
      o = o || {};
      var pre = 'eod2v3:' + (o.datasetId || '');
      return cache.del(function (k) {
        var p = String(k).split(':');
        return (!o.datasetId || p[1] === o.datasetId) && (!o.symbol || p[2] === o.symbol) && String(k).indexOf('eod2v3:') === 0 && pre.length >= 0;
      });
    }
    var testHooks = {
      corruptCache: async function (symbol) {
        var ks = await cache.keys(), n = 0;
        for (var i = 0; i < ks.length; i++) if (String(ks[i]).split(':')[2] === symbol) { var b = await cache.get(ks[i]); var c = new Uint8Array(b); c[c.length >> 1] = (c[c.length >> 1] + 1) & 255; await cache.put(ks[i], c); n++; }
        return n;
      },
      cacheKeys: function () { return cache.keys(); }
    };
    return Object.freeze({ openSnapshot: openSnapshot, clearCache: clearCache, __test: testHooks, cacheKind: cache.kind });
  }

  root.EOD2Client = Object.freeze({ create: create, VERSION: VERSION, CONTRACT: CONTRACT });
})(typeof globalThis !== 'undefined' ? globalThis : (typeof self !== 'undefined' ? self : this));
