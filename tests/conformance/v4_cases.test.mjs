// v4 conformance (Backtest Lab plan r2 section 6): the SAME client file must give the SAME answers when the data of each v3 case is served
// in the v4 layout (content-addressed history file + per-kind tail). Each v3 case root is converted in memory; wire defects (a file whose
// bytes do not match the manifest) are carried over. Cases that script v3 paths or cache keys (14, 15, 16) get v4 tests of their own below.
// Prints nothing on success.   node tests/conformance/v4_cases.test.mjs [casesDir]
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { EOD2Client } from '../../clients/eod2-client.mjs';

const CASES = process.argv[2] || 'C:/dev/fixtures/cases';
const BASE = 'https://cases.test/md/';
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const enc = p => p.split('/').map(encodeURIComponent).join('/');
const bytesOf = o => Buffer.from(JSON.stringify(o), 'utf8');

/** v3 case root -> {url path under data/: Buffer} in the v4 layout. `cut` = how many bars go to the history file. */
export function toV4(root, cut = 45) {
  const m3 = JSON.parse(fs.readFileSync(path.join(root, 'data', 'manifest.json'), 'utf8'));
  const sdir = path.join(root, 'data', m3.snapshotBase);
  const wire = {}, files = {}, tailSeries = {};
  let histEnd = null;
  for (const [key, f] of Object.entries(m3.files)) {
    const raw = fs.readFileSync(path.join(sdir, f.path));
    const tampered = sha(raw) !== f.sha256;                       // the case serves bytes that differ from its manifest
    const doc = JSON.parse(raw.toString('utf8'));
    const bars = doc.bars, hb = bars.slice(0, Math.min(cut, bars.length)), tb = bars.slice(hb.length);
    if (histEnd === null && hb.length && typeof hb[hb.length - 1][0] === 'string') histEnd = hb[hb.length - 1][0];
    const hdoc = bytesOf({ ...doc, contract: 'alphadesk-eod2/4', bars: hb });
    const hs = sha(hdoc), rel = `hist/${f.kind}/${key}.${hs.slice(0, 12)}.json`;
    wire[enc(rel)] = tampered ? Buffer.concat([hdoc, Buffer.from(' ')]) : hdoc;
    (tailSeries[f.kind] ||= {})[key] = tb;
    const { path: _p, sha256: _s, bytes: _b, ...rest } = f;
    files[key] = { ...rest, hist: { path: rel, sha256: hs, bytes: hdoc.length, bars: hb.length, first: hb[0][0], last: hb[hb.length - 1][0] }, tailBars: tb.length, bars: bars.length };
  }
  const tails = {};
  for (const [kind, series] of Object.entries(tailSeries)) {
    const b = bytesOf({ kind, datasetId: m3.datasetId, from: 'x', series }), rel = `tail/${kind}.${m3.datasetId}.json`;
    wire[enc(rel)] = b; tails[kind] = { path: rel, sha256: sha(b), bytes: b.length };
  }
  const cal = fs.readFileSync(path.join(sdir, m3.calendar.path));
  const crel = `cal/${m3.calendar.sha256.slice(0, 12)}/calendar.json`;
  wire[enc(crel)] = cal;                                           // manifest keeps the case's sha: a tampered calendar stays tampered
  const m4 = { ...m3, contract: 'alphadesk-eod2/4', snapshotBase: '', histEnd: histEnd || '2025-03-01', adjustmentPolicy: 'eod2-split-bonus+reviewed-actions',
    tails, files, calendar: { ...m3.calendar, path: crel }, pendingActions: {} };
  wire['manifest.json'] = bytesOf(m4);
  return { wire, manifest: m4 };
}

export function wireFetch(wire, log) {
  return async url => {
    const u = String(url);
    assert.ok(u.startsWith(BASE + 'data/'), 'unexpected url ' + u);
    const rel = u.slice((BASE + 'data/').length).split('?')[0];
    if (log) log.push(rel);
    const b = wire[rel];
    if (!b) return { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
    return { ok: true, status: 200, arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) };
  };
}

async function runSteps(spec, client) {
  const handles = {}, out = [], failOf = r => ({ ok: false, code: r.code });
  for (const s of spec.steps) {
    const h = handles[s.on || 'h'];
    if (s.op === 'open') { const r = await client.openSnapshot(); if (!r.ok) { out.push(failOf(r)); continue; } handles[s.as || 'h'] = r; out.push({ ok: true, id: r.id, sessions: r.calendar.sessions.length }); }
    else if (s.op === 'getSeries') { const r = await h.getSeries(s.symbol, { force: !!s.force }); out.push(r.ok ? { ok: true, bars: r.bars.length, first: r.meta.first, last: r.meta.last, lastClose: r.bars[r.bars.length - 1][4], cache: r.meta.cacheStatus, fromCache: r.meta.fromCache, smallOhlc: r.meta.smallOhlcDiscrepancies } : failOf(r)); }
    else if (s.op === 'checkCoverage') { const r = h.checkCoverage(s.symbol, s.opts); out.push({ ok: r.ok, code: r.code, reasons: r.reasons, missing: r.missing, firstTradable: r.firstTradable }); }
    else if (s.op === 'verifyAll') { const r = await h.verifyAll({ force: !!s.force }); out.push({ ok: r.ok, checked: r.checked, failures: r.failures.map(f => [f.symbol, f.code]) }); }
    else throw new Error('op not convertible: ' + s.op);
  }
  return out;
}

const GENERIC = ['01_valid', '02_hash_mismatch', '03_invalid_date', '04_nonfinite', '05_missing_session', '06_not_yet_listed', '07_listing_unverified', '08_quarantined',
  '09_excluded', '10_calendar_beyond_verified', '11_calendar_hash_tamper', '12_warmup_insufficient', '13_missing_execution_open', '17_close_only_index', '18_migrated_approval'];
let checked = 0;
for (const name of GENERIC) {
  const dir = path.join(CASES, name), spec = JSON.parse(fs.readFileSync(path.join(dir, 'case.json'), 'utf8'));
  for (const cut of [45, 1000]) {                                  // 45: history + tail; 1000: everything in the history file, empty tail rows
    const { wire } = toV4(path.join(dir, 'root'), cut);
    const client = EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: wireFetch(wire) });
    assert.deepEqual(await runSteps(spec, client), spec.expected, `${name} (v4, cut ${cut})`);
    checked++;
  }
}

// ── v4-only behaviour ────────────────────────────────────────────────────────────────────────────────────────────────────
const root01 = path.join(CASES, '01_valid', 'root');
{ // cache: history by content hash, tail once per handle; a second handle downloads nothing but the manifest and calendar
  const { wire } = toV4(root01), log = [], cache = memCache();
  const mk = () => EOD2Client.create({ baseUrl: BASE, cache, fetch: wireFetch(wire, log) });
  const h1 = await mk().openSnapshot();
  assert.equal(h1.contract, 'alphadesk-eod2/4');
  const a = await h1.getSeries('A1E'), b = await h1.getSeries('B1E');
  assert.ok(a.ok && b.ok && a.meta.cacheStatus === 'MISS' && Object.isFrozen(a.bars) && Object.isFrozen(a.bars[0]));
  assert.equal(log.filter(u => u.startsWith('tail/ETF')).length, 1, 'one tail download for two ETFs');
  const n = log.length, h2 = await mk().openSnapshot(), a2 = await h2.getSeries('A1E');
  assert.ok(a2.ok && a2.meta.fromCache && a2.meta.cacheStatus === 'HIT');
  assert.deepEqual(log.slice(n).map(u => u.split('/')[0]).sort(), ['cal', 'manifest.json'], 'second open: manifest + calendar only');
  assert.deepEqual(a2.bars, a.bars);
  // tampered cache entry: re-hash fails, entry discarded, refetched
  const hk = (await cache.keys()).find(k => k.startsWith('eod2v4:hist:A1E:'));
  const bad = new Uint8Array(await cache.get(hk)); bad[10] ^= 1; await cache.put(hk, bad);
  const h3 = await mk().openSnapshot(), a3 = await h3.getSeries('A1E');
  assert.ok(a3.ok && a3.meta.cacheStatus === 'REPAIRED' && !a3.meta.fromCache);
  assert.deepEqual(a3.bars, a.bars);
  // clearCache by symbol removes that history file only; without a filter everything
  assert.equal(await mk().clearCache({ symbol: 'A1E' }), 1);
  assert.ok((await mk().clearCache({})) >= 2);
}
{ // a tail that changes on the wire is refused for every symbol of that kind; other kinds are unaffected
  const { wire, manifest } = toV4(root01);
  const k = enc(manifest.tails.ETF.path); wire[k] = Buffer.concat([wire[k], Buffer.from(' ')]);
  const h = await EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: wireFetch(wire) }).openSnapshot();
  assert.equal((await h.getSeries('A1E')).code, 'HASH_MISMATCH'); assert.equal((await h.getSeries('B1E')).code, 'HASH_MISMATCH');
  assert.ok((await h.getSeries('NIFTY 50')).ok);
}
{ // verifyAll: cached = no network; force re-reads the network and finds a file tampered after it was cached
  const { wire, manifest } = toV4(root01), cache = memCache(), log = [];
  const mk = () => EOD2Client.create({ baseUrl: BASE, cache, fetch: wireFetch(wire, log) });
  const h1 = await mk().openSnapshot();
  assert.deepEqual(await h1.verifyAll(), { ok: true, checked: 3, failures: [] });
  const k = enc(manifest.files.B1E.hist.path); wire[k] = Buffer.concat([wire[k], Buffer.from(' ')]);
  const h2 = await mk().openSnapshot(), n = log.length;
  assert.equal((await h2.verifyAll()).ok, true); assert.equal(log.length, n, 'unforced verify is served from the cache');
  const v = await h2.verifyAll({ force: true });
  assert.deepEqual(v.failures.map(f => [f.symbol, f.code]), [['B1E', 'HASH_MISMATCH']]);
  assert.equal(log.slice(n).filter(u => u.startsWith('tail/ETF')).length, 1, 'forced verify re-reads a tail once, not once per symbol');
}
{ // a new dataset: history files that did not change are reused from the cache, entries whose hash left the manifest are purged
  const a = toV4(root01, 45), b = toV4(root01, 46), cache = memCache(), log = [];        // b: every history file differs (one more bar)
  const h1 = await EOD2Client.create({ baseUrl: BASE, cache, fetch: wireFetch(a.wire) }).openSnapshot();
  await h1.getMany(['A1E', 'B1E', 'NIFTY 50']);
  const before = await cache.keys();
  assert.equal(before.filter(k => k.startsWith('eod2v4:hist:')).length, 3);
  const h2 = await EOD2Client.create({ baseUrl: BASE, cache, fetch: wireFetch(b.wire, log) }).openSnapshot();
  const after = await cache.keys();
  assert.equal(after.filter(k => before.includes(k)).length, 0, 'stale entries purged on open');
  const many = await h2.getMany(['A1E', 'NOPE', 'NIFTY_50'], { concurrency: 2 });
  assert.ok(many.A1E.ok && many.NIFTY_50.ok && many.NOPE.code === 'NOT_IN_EXPORT');
  assert.equal(many.A1E.bars.length, 80);
  const prog = []; await h2.getMany(['A1E', 'B1E'], { onProgress: (d, t) => prog.push(d + '/' + t) });
  assert.deepEqual(prog, ['1/2', '2/2']);
}
{ // symbol listed this year: no history file, bars come from the tail alone; names with & and spaces are URL-encoded
  const { wire, manifest } = toV4(root01, 45);
  const f = manifest.files.A1E, tailK = enc(manifest.tails.ETF.path), t = JSON.parse(wire[tailK].toString());
  const hist = JSON.parse(wire[enc(f.hist.path)].toString()).bars;
  t.series['M&M NEW'] = hist.concat(t.series.A1E);
  manifest.files['M&M NEW'] = { ...f, id: 'NSE:M&M NEW', hist: null, tailBars: 80, bars: 80 };
  const tb = bytesOf(t); wire[tailK] = tb; manifest.tails.ETF = { ...manifest.tails.ETF, sha256: sha(tb), bytes: tb.length };
  const idx = manifest.files['NIFTY 50'];
  assert.ok(Object.keys(wire).some(k => k.includes('NIFTY%2050.')) && idx.hist.path.includes('NIFTY 50.'), 'index path has a space, the wire key is encoded');
  wire['manifest.json'] = bytesOf(manifest);
  const h = await EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: wireFetch(wire) }).openSnapshot();
  const r = await h.getSeries('M&M NEW');
  assert.ok(r.ok && r.bars.length === 80 && r.meta.sha256 === manifest.tails.ETF.sha256);
  assert.ok((await h.getSeries('NIFTY 50')).ok);
}
{ // manifest says N tail bars, the tail holds fewer: refused
  const { wire, manifest } = toV4(root01, 45);
  manifest.files.A1E.tailBars += 1; manifest.files.A1E.bars += 1; wire['manifest.json'] = bytesOf(manifest);
  const h = await EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: wireFetch(wire) }).openSnapshot();
  assert.equal((await h.getSeries('A1E')).code, 'SCHEMA_INVALID');
}
function memCache() {
  const m = new Map();
  return { kind: 'memory', get: async k => (m.has(k) ? m.get(k) : null), put: async (k, b) => { m.set(k, b); }, keys: async () => [...m.keys()],
    del: async pred => { let n = 0; for (const k of [...m.keys()]) if (pred(k)) { m.delete(k); n++; } return n; } };
}
if (process.argv.includes('--count')) console.log(checked + ' converted case runs + 6 v4 blocks');
