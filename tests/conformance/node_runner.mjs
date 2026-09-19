// Conformance runner (Node) for clients/eod2-client.js. Prints {caseName: [normalized results]} as JSON.
//   node tests/conformance/node_runner.mjs [casesDir] [caseName...]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { EOD2Client } from '../../clients/eod2-client.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));

function makeFetch(caseDir, baseUrl, state) {
  return async url => {
    let rel = String(url);
    if (!rel.startsWith(baseUrl)) throw new Error('unexpected url ' + url);
    rel = rel.slice(baseUrl.length).split('?')[0];
    let file;
    if (state.overrides[rel]) file = state.overrides[rel];
    else if (rel === 'data/manifest.json' && state.pointer) file = state.pointer;
    else file = path.join(caseDir, 'root', rel);
    if (!fs.existsSync(file)) return { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
    const b = fs.readFileSync(file);
    return { ok: true, status: 200, arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) };
  };
}

const resolveFile = (caseDir, f) => (f.startsWith('@') ? path.join(caseDir, f.slice(1)) : path.join(caseDir, 'root', f));
const failOf = r => ({ ok: false, code: r.code });

export async function runCase(caseDir) {
  const spec = JSON.parse(fs.readFileSync(path.join(caseDir, 'case.json'), 'utf8'));
  const state = { pointer: null, overrides: {} };
  const client = EOD2Client.create({ baseUrl: spec.baseUrl, cache: 'memory', fetch: makeFetch(caseDir, spec.baseUrl, state) });
  const handles = {};
  const out = [];
  for (const s of spec.steps) {
    const h = handles[s.on || 'h'];
    switch (s.op) {
      case 'open': {
        const r = await client.openSnapshot();
        if (!r.ok) { out.push(failOf(r)); break; }
        handles[s.as || 'h'] = r;
        out.push({ ok: true, id: r.id, sessions: r.calendar.sessions.length });
        break;
      }
      case 'getSeries': {
        const r = await h.getSeries(s.symbol, { force: !!s.force });
        out.push(r.ok ? { ok: true, bars: r.bars.length, first: r.meta.first, last: r.meta.last, lastClose: r.bars[r.bars.length - 1][4], cache: r.meta.cacheStatus, fromCache: r.meta.fromCache, smallOhlc: r.meta.smallOhlcDiscrepancies } : failOf(r));
        break;
      }
      case 'checkCoverage': {
        const r = h.checkCoverage(s.symbol, s.opts);
        out.push({ ok: r.ok, code: r.code, reasons: r.reasons, missing: r.missing, firstTradable: r.firstTradable });
        break;
      }
      case 'verifyAll': {
        const r = await h.verifyAll({ force: !!s.force });
        out.push({ ok: r.ok, checked: r.checked, failures: r.failures.map(f => [f.symbol, f.code]) });
        break;
      }
      case 'switchPointer': state.pointer = resolveFile(caseDir, s.file); out.push({ done: true }); break;
      case 'serveOverride': state.overrides[s.path] = resolveFile(caseDir, s.file); out.push({ done: true }); break;
      case 'corruptCache': out.push({ corrupted: await client.__test.corruptCache(s.symbol) }); break;
      case 'clearCache': out.push({ cleared: await client.clearCache(s.opts) }); break;
      case 'cacheKeys': out.push({ keys: (await client.__test.cacheKeys()).map(k => '@sha:' + String(k).split(':')[2]).sort() }); break;
      default: throw new Error('unknown op ' + s.op);
    }
  }
  return out;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const dir = process.argv[2] || 'C:/dev/fixtures/cases';
  const only = process.argv.slice(3);
  const res = {};
  for (const name of fs.readdirSync(dir).sort()) {
    if (only.length && !only.includes(name)) continue;
    res[name] = await runCase(path.join(dir, name));
  }
  process.stdout.write(JSON.stringify(res));
}
void HERE;
