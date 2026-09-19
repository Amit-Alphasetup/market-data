# EOD2 clients (contract `alphadesk-eod2/3`)

Two clients with identical semantics, proven equal by the 18 conformance cases (`tests/test_d4_conformance.py`):

| File | Use |
|---|---|
| `eod2-client.js` | Classic `<script>`; defines `globalThis.EOD2Client`. Browser (AlphaDesk, StratLab). |
| `eod2-client.mjs` | ES-module wrapper for Node ≥ 18 (`import { EOD2Client } from './eod2-client.mjs'`). |
| `eod2_client.py` | Python 3 stdlib only (NSE backtester). |

## Rules every consumer follows

1. `openSnapshot()` **once** per session. The handle is frozen and pinned to one `datasetId`; it never re-reads `manifest.json`, so a mid-run publish cannot mix two datasets.
2. `getSeries(symbol)` **before** `checkCoverage(...)`. `checkCoverage` is synchronous and answers `SOURCE_UNAVAILABLE` for a series that has not been loaded.
3. Every call returns `{ok:false, code, detail}` on a data problem. The clients never throw for data problems; only programmer errors throw.
4. Check `ok` and `code` (reason codes are in `CONTRACT.md` §4.4). Never treat "no bars" as "no trading".
5. Bars are `[date, open, high, low, close, volume]`, deeply frozen. Do not mutate them; copy first.
6. Honour `meta.noTradeDays` (dates that are sessions in the calendar but have no traded bar for that symbol). Do not forward-fill them silently.
7. Hashing is over the raw response bytes. Never re-serialise a file before verifying it.

## JavaScript (browser)

```html
<script src="clients/eod2-client.js"></script>
<script>
  const client = EOD2Client.create({ baseUrl: 'https://amit-alphasetup.github.io/market-data/', cache: 'indexeddb' });
  const snap = await client.openSnapshot();
  if (!snap.ok) return show(snap.code, snap.detail);
  const s = await snap.getSeries('NIFTYBEES');
  if (!s.ok) return show(s.code, s.detail);
  const cov = snap.checkCoverage('NIFTYBEES', { from: '2024-01-01', to: '2025-01-01', warmupSessions: 200, fields: ['close'] });
  if (!cov.ok) return show(cov.code, cov.reasons);
  // s.bars, s.meta.cacheStatus: MISS | HIT | REPAIRED | BYPASSED
</script>
```

Cache options: `'indexeddb'` (browser default when available), `'memory'`, `null`. A cache hit is re-hashed against the manifest before use; a mismatch is refetched and reported as `REPAIRED`. `client.clearCache({datasetId, symbol})` clears entries. `snap.verifyAll({force, onProgress})` verifies every file in the snapshot.

## Node

```js
import { EOD2Client } from './clients/eod2-client.mjs';
const snap = await EOD2Client.create({ baseUrl, cache: 'memory' }).openSnapshot();
```

## Python

```python
from eod2_client import EOD2Client
snap = EOD2Client(base_url, cache="C:/cache/eod2_v3").open_snapshot()
if isinstance(snap, dict):            # {"ok": False, "code": ..., "detail": ...}
    raise SystemExit(snap["code"])
s = snap.get_series("NIFTYBEES")
cov = snap.check_coverage("NIFTYBEES", from_="2024-01-01", to="2025-01-01", warmup_sessions=200)
```

`base_url` may also be a local folder or `file://` URL that holds a `data/` tree (useful for offline runs). Directory cache files are named `eod2v3__<datasetId>__<SYMBOL>__<sha256>` and are re-hashed on every hit.

## `checkCoverage` order of decisions

1. manifest status → 2. series not loaded (`SOURCE_UNAVAILABLE`) → 3. `CALENDAR_UNVERIFIED` → 4. `STALE_DATASET` → 5. certified-mode warn eligibility → 6. `MISSING_EXPECTED_SESSION` → 7. `MISSING_REQUIRED_FIELD` → 8. `MISSING_EXECUTION_OPEN` (`execDates`) → 9. `INSUFFICIENT_WARMUP`.

## Tests

- `py -m pytest tests/test_d4_conformance.py tests/test_d4_client_extras.py`
- Real Chrome (classic script + IndexedDB): `node --test C:\dev\alphadesk-harness\browser\client.test.mjs`
