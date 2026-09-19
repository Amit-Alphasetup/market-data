// Extra tests for the JS client (node:test): immutability, no-throw contract, manifest never reloaded, packaging (classic script + ESM wrapper).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { EOD2Client } from '../../clients/eod2-client.mjs';
import EOD2Default from '../../clients/eod2-client.mjs';

const CASE = 'C:/dev/fixtures/cases/01_valid';
const BASE = 'https://cases.test/md/';

function fetcher(log, over = {}) {
  return async url => {
    log.push(String(url));
    const rel = String(url).slice(BASE.length).split('?')[0];
    if (over[rel] === 'THROW') throw new Error('network down');
    if (over[rel] !== undefined) return over[rel];
    const f = `${CASE}/root/${rel}`;
    if (!fs.existsSync(f)) return { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
    const b = fs.readFileSync(f);
    return { ok: true, status: 200, arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) };
  };
}
const resp = (text, ok = true, status = 200) => ({ ok, status, arrayBuffer: async () => new TextEncoder().encode(text).buffer });

test('bars are deeply frozen and results cannot be mutated', async () => {
  const c = EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: fetcher([]) });
  const snap = await c.openSnapshot();
  assert.equal(snap.ok, true); assert.ok(Object.isFrozen(snap));
  const r = await snap.getSeries('A1E');
  assert.ok(Object.isFrozen(r) && Object.isFrozen(r.bars) && Object.isFrozen(r.meta) && Object.isFrozen(r.meta.reasons));
  assert.ok(r.bars.every(Object.isFrozen));
  assert.throws(() => { r.bars.push([]); }, TypeError);
  assert.throws(() => { r.bars[0][4] = 1; }, TypeError);
  assert.throws(() => { r.bars[0] = []; }, TypeError);
  assert.throws(() => { r.meta.first = 'x'; }, TypeError);
  assert.throws(() => { snap.getSeries = null; }, TypeError);
  assert.equal((await snap.getSeries('A1E')), r, 'a handle memoizes the series');
});

test('data problems never throw: network errors, 404, junk, unpublishable', async () => {
  const cases = [
    ['network down', { 'data/manifest.json': 'THROW' }, 'SOURCE_UNAVAILABLE'],
    ['404', { 'data/manifest.json': resp('', false, 404) }, 'SOURCE_UNAVAILABLE'],
    ['not JSON', { 'data/manifest.json': resp('<html>oops</html>') }, 'SCHEMA_INVALID'],
    ['NaN literal', { 'data/manifest.json': resp('{"contract":NaN}') }, 'SCHEMA_INVALID'],
    ['wrong contract', { 'data/manifest.json': resp('{"contract":"other/1"}') }, 'SCHEMA_INVALID'],
    ['unpublishable', { 'data/manifest.json': resp(JSON.stringify({ ...JSON.parse(fs.readFileSync(CASE + '/root/data/manifest.json', 'utf8')), publishStatus: 'UNPUBLISHABLE' })) }, 'SOURCE_UNAVAILABLE'],
    ['manifest missing fields', { 'data/manifest.json': resp('{"contract":"alphadesk-eod2/3","publishStatus":"PUBLISHABLE"}') }, 'SCHEMA_INVALID'],
  ];
  for (const [name, over, code] of cases) {
    const r = await EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: fetcher([], over) }).openSnapshot();
    assert.equal(r.ok, false, name); assert.equal(r.code, code, name); assert.equal(typeof r.detail, 'string');
  }
  const id = JSON.parse(fs.readFileSync(CASE + '/root/data/manifest.json', 'utf8')).datasetId;
  const c = EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: fetcher([], { [`data/snapshots/${id}/etf/A1E.json`]: 'THROW', [`data/snapshots/${id}/etf/B1E.json`]: resp('not json') }) });
  const snap = await c.openSnapshot();
  assert.deepEqual([(await snap.getSeries('A1E')).code, (await snap.getSeries('B1E')).code], ['SOURCE_UNAVAILABLE', 'HASH_MISMATCH']);
  assert.equal(snap.checkCoverage('A1E', { from: 'bad', to: '2025-01-02' }).code, 'SCHEMA_INVALID');
  assert.equal(snap.checkCoverage('A1E', { from: '2025-02-01', to: '2025-01-02' }).code, 'SCHEMA_INVALID');
});

test('a handle never reloads the manifest and stays under its own snapshotBase', async () => {
  const log = [];
  const c = EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: fetcher(log) });
  const snap = await c.openSnapshot();
  await snap.verifyAll({ force: true });
  await snap.verifyAll();
  const m = log.filter(u => u.includes('manifest.json'));
  assert.equal(m.length, 1, 'exactly one manifest fetch for the whole life of the handle: ' + JSON.stringify(m));
  const base = BASE + 'data/' + snap.manifest.snapshotBase;
  assert.ok(log.filter(u => !u.includes('data/manifest.json')).every(u => u.startsWith(base)), 'every other fetch is under the snapshot folder');
});

test('force re-fetches and re-hashes; the non-force path serves the cache; progress is reported', async () => {
  const log = [];
  const c = EOD2Client.create({ baseUrl: BASE, cache: 'memory', fetch: fetcher(log) });
  const snap = await c.openSnapshot();
  const prog = [];
  await snap.verifyAll({ onProgress: (i, n) => prog.push([i, n]) });
  const after1 = log.length;
  const snap2 = await c.openSnapshot();
  await snap2.verifyAll();
  assert.equal(log.length - after1, 2 /* manifest + calendar only */, 'second handle reads everything from the cache');
  const before = log.length;
  await snap2.verifyAll({ force: true });
  assert.equal(log.length - before, 3, 'force fetches every file again');
  assert.deepEqual(prog, [[1, 3], [2, 3], [3, 3]]);
});

test('packaging: classic script defines a global without `export`; the .mjs wrapper re-exports it', async () => {
  const src = fs.readFileSync('C:/dev/market-data/clients/eod2-client.js', 'utf8');
  assert.ok(!/^\s*(export|import)\s/m.test(src), 'no ES module syntax in the classic file');
  const sandbox = { crypto: webcrypto, TextDecoder, TextEncoder, console };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);            // classic <script> semantics
  assert.equal(typeof sandbox.EOD2Client.create, 'function'); assert.equal(sandbox.EOD2Client.CONTRACT, 'alphadesk-eod2/3');
  assert.ok(Object.isFrozen(sandbox.EOD2Client));
  assert.equal(EOD2Client, globalThis.EOD2Client); assert.equal(EOD2Default, EOD2Client);
  assert.equal(fs.readFileSync('C:/dev/market-data/clients/eod2-client.mjs', 'utf8').trim(), "import './eod2-client.js'; export const EOD2Client = globalThis.EOD2Client; export default EOD2Client;");
});

test('clearCache filters by dataset and symbol; baseUrl gets a trailing slash', async () => {
  const c = EOD2Client.create({ baseUrl: BASE.slice(0, -1), cache: 'memory', fetch: fetcher([]) });
  const snap = await c.openSnapshot();
  assert.equal(snap.ok, true);
  await snap.verifyAll();
  assert.equal(await c.clearCache({ symbol: 'A1E' }), 1);
  assert.equal(await c.clearCache({ datasetId: 'nope' }), 0);
  assert.equal(await c.clearCache({ datasetId: snap.id }), 2);
  assert.equal(await c.clearCache(), 0);
});
