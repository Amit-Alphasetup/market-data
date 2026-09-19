## 4. DATA CONTRACT v3 (SINGLE SOURCE OF TRUTH FOR ALL APPS)

Contract ID: `"alphadesk-eod2/3"`. Producers and consumers must check it. D0 writes this whole §4 verbatim into `market-data/CONTRACT.md`.

### 4.1 Instrument registry — `C:\dev\market-data\config\universe.json`

```json
{
  "version": 3,
  "defaultHistoryStart": "2019-01-14",
  "calendar": { "cutoffIST": "19:15", "holidaySource": "eod2_meta" },
  "instruments": [
    {
      "id": "NSE:NIFTYBEES",
      "symbol": "NIFTYBEES",
      "kind": "ETF",
      "exchange": "NSE",
      "isin": "",
      "name": "Nippon India ETF Nifty 50 BeES",
      "assetClass": "equity",
      "engines": ["LIFO", "DM", "TENET"],
      "linkedIndex": "NIFTY 50",
      "listingDate": null,
      "listingDateEvidence": null,
      "delistingDate": null,
      "aliases": [],
      "eod2File": null,
      "historyStart": null,
      "enabled": true
    }
  ],
  "indices": [
    { "id": "IDX:NIFTY 50", "name": "NIFTY 50", "kind": "INDEX", "eod2File": null,
      "requiredFields": ["open","high","close"], "historyStart": null, "enabled": true }
  ],
  "splits": [],
  "genuineMoves": [],
  "quarantine": [],
  "gapPolicy": {}
}
```

Rules:
- `id` permanent. Old symbols → `aliases: [{"symbol":"OLD","from":"YYYY-MM-DD","to":"YYYY-MM-DD"}]`.
- `isin` may be empty → WARN `ISIN_MISSING` (see §4.4 eligibility).
- `assetClass` ∈ `equity | gold | silver | international | debt | liquid`.
- `engines` ⊆ `["LIFO","DM","TENET"]`; may be empty (data-only).
- `eod2File` null → resolver (D1 step 3).
- `listingDate` is **evidence-only**: set only from an NSE source Apd supplies, with `listingDateEvidence` text. **Never derived from the first bar.**
- Index `requiredFields` default `["open","high","close"]`; `low` optional. D0 migration sets each index's list to the fields verified in the 2026-09-11 certification (read from the certify report / v2 config; if not found → O/H/C). An index whose older history is close-only gets `historyStart` = its first date with all required fields (applies to NIFTY MIDCAP 150).
- `splits[]` entry: `{symbol, date, ratioNum, ratioDen, evidence, approvedAt, provenance}`.
- `genuineMoves[]` entry: `{symbol, date, evidence, approvedAt, provenance}`. Approvals are **per date**; a new anomaly date is never covered by an older approval.
- Entries migrated from v2 keep their real date and get `evidence:null, approvedAt:null, provenance:"migrated-v2-2026-09-11"`. Never write placeholder evidence text or a fabricated approval date. New approvals (via CLI) require non-empty evidence and set `provenance:"dataops"`.
- `quarantine`, `gapPolicy`: copied verbatim from v2.

### 4.2 Published repo layout

```
market-data/                     (GitHub Pages root)
  index.html                     control panel (D3)
  CONTRACT.md
  data/
    manifest.json                POINTER = byte copy of latest publishable snapshot manifest
    snapshots/<datasetId>/
        manifest.json
        etf/<SYMBOL>.json
        index/<NAME_WITH_UNDERSCORES>.json
        calendar.json
        report.json
    control/
      status.json
      processed_ids.json
      requests/                  written by panel only
      inflight/                  written by poller only
      history/                   written by poller only
  clients/
    eod2-client.js               classic IIFE (D4)
    eod2-client.mjs              ESM wrapper (D4)
    eod2_client.py               (D4)
    README.md
```

`datasetId` = `YYYY-MM-DD-r<N>`: date = `latestCompletedSession`; N = 1 + max existing N for that date across `work/` and `data/snapshots/`, computed **inside the lock**.

### 4.3 Manifest v3 (`snapshots/<id>/manifest.json`)

```json
{
  "contract": "alphadesk-eod2/3",
  "dataset": "EOD2",
  "datasetId": "2026-09-18-r1",
  "datasetHash": "<hex, §4.6>",
  "policyHash": "<hex, §4.6>",
  "snapshotBase": "snapshots/2026-09-18-r1/",
  "createdAt": "2026-09-18T19:41:07+05:30",
  "exporterVersion": "3.0.0",
  "latestSession": "2026-09-18",
  "latestCompletedSession": "2026-09-18",
  "windowStart": "2019-01-14",
  "publishStatus": "PUBLISHABLE",
  "certification": "PARTIAL",
  "providerMixing": false,
  "fallbackUsed": false,
  "holidays": ["2026-01-26", "..."],
  "calendar": { "id": "nse-eq-2026-09-18", "path": "calendar.json", "sha256": "<hex>", "bytes": 12345,
                "cutoffIST": "19:15", "coverageFrom": "2019-01-01", "coverageTo": "2026-12-31",
                "verifiedThrough": "2026-12-31" },
  "adjustmentPolicy": "eod2-split-adjusted-price-only",
  "files": {
    "NIFTYBEES": {
      "id": "NSE:NIFTYBEES", "kind": "ETF", "path": "etf/NIFTYBEES.json",
      "sha256": "<hex>", "bytes": 123456, "bars": 1850,
      "first": "2019-01-14", "last": "2026-09-18",
      "firstObservedDate": "2019-01-14",
      "listingDate": null, "listingDateSource": "none",
      "fields": ["date","open","high","low","close","volume"],
      "assetClass": "equity", "engines": ["LIFO","DM","TENET"], "linkedIndex": "NIFTY 50",
      "isin": "", "status": "WARN",
      "reasons": [{"code":"ISIN_MISSING"}]
    }
  },
  "excluded":    { "SYMBOL": { "code": "NOT_IN_EOD2", "detail": "..." } },
  "quarantined": { "MONQ50": { "code": "ANOMALOUS_MOVE_UNRESOLVED",
                               "events": [{"date":"2026-09-16","prevClose":0,"close":0,"ret":0.198}] } },
  "summary": { "requested": 46, "ok": 43, "warn": 1, "excluded": 1, "quarantined": 1 }
}
```

**Three separate levels — never merge them:**
1. **Publication integrity** (`publishStatus`): `UNPUBLISHABLE` only if a written file fails its sha256 re-check, calendar missing/invalid, manifest schema invalid, or zero instruments usable. A broken/quarantined individual ETF or index **never** blocks publication.
2. **Instrument readiness**: `files[X].status ∈ OK | WARN` with `reasons[]`; blocked instruments appear only in `excluded`/`quarantined`.
3. **Run certification**: decided by the consuming app per run from that run's dependencies (§7 A2b). Unrelated symbols never affect it.

`certification` (legacy key, display only): `PASS` = no excluded/quarantined; `PARTIAL` = publishable with ≥1 excluded/quarantined. **No consumer may gate on it.** v321 keys kept: `dataset, datasetHash, latestSession, windowStart, certification, providerMixing, fallbackUsed, holidays, files, quarantined`. (v321 itself keeps reading the legacy mirror, AD8, not this manifest.)

Consumers resolve paths as `baseUrl + "data/" + snapshotBase + files[X].path`.

### 4.4 Reason codes (identical in Python and JS — copy verbatim)

Level meanings: **block** = instrument unusable; **warn** = usable, eligibility per last column; **info** = no effect.

| Code | Level | Meaning | Allowed in a CERTIFIED run? |
|---|---|---|---|
| OK | ok | valid | yes |
| NOT_YET_LISTED | info | date before verified listing | yes (candidate eligible later) |
| DELISTED_BEFORE_WINDOW | info | delisted before window | yes |
| ISIN_MISSING | warn | registry has no ISIN | yes — listed in run provenance |
| SMALL_OHLC_DISCREPANCY | warn | OHLC inconsistency ≤ 0.05% | yes — listed in provenance |
| APPROVAL_EVIDENCE_MISSING | warn | exception has no recorded evidence | yes **only if** every such exception has `provenance:"migrated-v2-2026-09-11"`; otherwise no |
| LISTING_DATE_UNVERIFIED | warn | no verified listingDate and first bar after windowStart | yes **only if** run window for that symbol starts on/after `firstObservedDate`; otherwise no |
| NOT_IN_EOD2 | block | EOD2 has no file | no |
| NOT_IN_EXPORT | block | not in this snapshot | no |
| EXPORT_FAILED | block | exporter error | no |
| IDENTITY_MISMATCH | block | ISIN/series/kind mismatch | no |
| AMBIGUOUS_SYMBOL | block | >1 candidate file | no |
| WRONG_EXCHANGE_OR_KIND | block | ETF/INDEX/EQ mismatch | no |
| HASH_MISMATCH | block | bytes ≠ manifest sha256 | no |
| SCHEMA_INVALID | block | violates §4.5 | no |
| INVALID_DATE | block | bad date | no |
| NONFINITE_VALUE | block | NaN/Infinity/string number | no |
| MISSING_REQUIRED_FIELD | block | required field null | no |
| OHLC_INCONSISTENT | block | inconsistency > 0.05% | no |
| MISSING_EXPECTED_SESSION | block | session missing inside coverage range | no |
| INSUFFICIENT_WARMUP | block | too few bars before report start | no |
| MISSING_EXECUTION_OPEN | block | execution-session OPEN absent | no |
| ADJUSTMENT_POLICY_UNKNOWN | block | adjustment unknown | no |
| CORPORATE_ACTION_UNRESOLVED | block | split suspected, not in registry | no |
| ANOMALOUS_MOVE_UNRESOLVED | block | \|ret\| ≥ 15% not approved for that date | no |
| DATASET_VERSION_CHANGED | block | snapshot changed mid-run | no |
| CALENDAR_UNVERIFIED | block | date beyond `verifiedThrough` / calendar hash bad | no |
| STALE_DATASET | block | latest session behind expected | no |
| SOURCE_UNAVAILABLE | block | unreachable | no |
| RUN_CONTEXT_MISSING | block | data call inside a run without ctx | no |
| SNAPSHOT_PROVENANCE_MISSING | block | supplied history not loader-owned | no |

Research mode allows every warn code; its result lists them.

### 4.5 Series file schema

```json
{
  "contract": "alphadesk-eod2/3",
  "provider": "EOD2",
  "id": "NSE:NIFTYBEES",
  "symbol": "NIFTYBEES",
  "kind": "ETF",
  "adjustment": "eod2-split-adjusted-price-only",
  "fields": ["date","open","high","low","close","volume"],
  "bars": [["2019-01-14", 187.1, 188.0, 186.5, 187.6, 1234567]]
}
```
Rules (exporter enforces, clients re-check):
1. Bars strictly ascending, no duplicate dates.
2. Date matches `^\d{4}-\d{2}-\d{2}$` and round-trips through a real date parser.
3. Numbers are JSON numbers, finite, prices > 0 (for fields present).
4. INDEX: fields not in `requiredFields` may be `null`; `fields` still lists all six (volume `null` allowed for indices).
5. `high ≥ max(open, close, low)`, `low ≤ min(open, close, high)` when present; violation ≤ 0.05% → WARN; > 0.05% → block.
6. Volume integer ≥ 0 (ETF).
7. UTF-8, no BOM, `\n` line endings; `sha256` over exact written bytes.
8. Bars include special sessions (AD11), flagged via calendar, not in the file.

### 4.6 Hashes (one byte-level rule)
- Every sha256 = over the **exact bytes** of the file as written and served. Clients hash the **raw response bytes** (`arrayBuffer`) with **no normalisation**.
- `policyHash = sha256(canonical_json({adjustmentPolicy, splits, genuineMoves, quarantine, gapPolicy}))`; canonical_json = `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")`.
- datasetHash:
```
lines = sorted(f"file:{k}:{v['sha256']}" for k, v in files.items())
lines += [f"calendar:{calendar_sha256}", f"policy:{policyHash}", "contract:alphadesk-eod2/3"]
datasetHash = sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
```

### 4.7 calendar.json
```json
{ "id": "nse-eq-2026-09-18", "timezone": "Asia/Kolkata", "cutoffIST": "19:15",
  "coverageFrom": "2019-01-01", "coverageTo": "2026-12-31", "verifiedThrough": "2026-12-31",
  "latestCompletedSession": "2026-09-18",
  "holidays": ["..."],
  "sessions": ["2019-01-01", "..."],
  "specialSessions": [ { "date": "2025-10-21", "kind": "MUHURAT" } ] }
```
- `sessions` = every regular trading day **coverageFrom → coverageTo** (full years whose holidays exist in EOD2 meta), including future days of the current year.
- `verifiedThrough` = coverageTo if that year's holiday list exists; else 31 Dec of the last covered year.
- `specialSessions` = sessions outside the regular list that actually traded (from EOD2 data: a bhavcopy exists on a weekend/holiday). They are valid bars; strategy use per AD11.
- Consumers: any date > `verifiedThrough` → `CALENDAR_UNVERIFIED`.
