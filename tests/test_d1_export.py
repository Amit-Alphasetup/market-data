"""D1: exporter v3 — the plan's D1 test list plus the edge cases found while building it. Synthetic EOD2 folders (tests/eod2_factory.py)."""
import copy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from dataops import export as ex
from dataops import hashing as H
from dataops import reasons as R
from eod2_factory import World, dataops_move, make_eod2, make_registry, migrated_move, walk, weekdays, write_registry

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 3, 10, 19, 41, 7, tzinfo=IST)


def build(tmp_path, world=None, reg=None, out="out", existing_roots=None, **root_kw):
    world = world or World(tmp_path)
    root = world.build(**root_kw)
    cfg = write_registry(tmp_path, reg or make_registry())
    outdir = os.path.join(str(tmp_path), out)
    res = ex.build_snapshot(root, cfg, outdir, now=NOW, existing_roots=existing_roots if existing_roots is not None else [outdir])
    return res, world


def bars_of(res, key):
    with open(os.path.join(res.out_dir, res.files[key]["path"]), encoding="utf-8") as f:
        return json.load(f)["bars"]


def jump(rows, i, factor):
    """Scale rows[i:] by `factor` so exactly one close-to-close jump appears at rows[i]."""
    out = list(rows[:i])
    for r in rows[i:]:
        out.append((r[0], round(r[1] * factor, 4), round(r[2] * factor, 4), round(r[3] * factor, 4), round(r[4] * factor, 4), r[5]))
    return out


# ── valid snapshot ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_valid_snapshot_is_publishable_and_hashes_match_bytes(tmp_path):
    res, w = build(tmp_path)
    m = res.manifest
    assert res.publish_status == "PUBLISHABLE" and m["publishStatus"] == "PUBLISHABLE"
    assert m["contract"] == "alphadesk-eod2/3" and m["dataset"] == "EOD2" and m["exporterVersion"] == "3.0.0"
    assert m["datasetId"] == w.latest + "-r1" and m["snapshotBase"] == "snapshots/%s/" % m["datasetId"] and m["latestCompletedSession"] == w.latest == m["latestSession"]
    assert m["providerMixing"] is False and m["fallbackUsed"] is False and m["certification"] == "PASS" and m["windowStart"] == "2025-06-02"
    assert m["createdAt"] == "2026-03-10T19:41:07+05:30"
    assert m["summary"] == {"requested": 5, "ok": 2, "warn": 3, "excluded": 0, "quarantined": 0}
    assert set(m["files"]) == {"AAA", "BBB", "CCC", "NIFTY 50", "NIFTY BANK"}
    for key, f in m["files"].items():
        data = open(os.path.join(res.out_dir, f["path"]), "rb").read()
        assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["bytes"]
        assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and not data.endswith(b"\n")
        doc = json.loads(data)
        assert doc["contract"] == "alphadesk-eod2/3" and doc["provider"] == "EOD2" and doc["fields"] == ["date", "open", "high", "low", "close", "volume"]
        assert doc["adjustment"] == "eod2-split-adjusted-price-only" and doc["kind"] == f["kind"] and doc["id"] == f["id"]
        dates = [b[0] for b in doc["bars"]]
        assert dates == sorted(set(dates)) and f["first"] == dates[0] and f["last"] == dates[-1] == w.latest and f["bars"] == len(dates)
        assert data == json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert m["files"]["AAA"]["path"] == "etf/AAA.json" and m["files"]["NIFTY BANK"]["path"] == "index/NIFTY_BANK.json"
    assert m["files"]["AAA"]["status"] == "WARN" and m["files"]["AAA"]["reasons"] == [{"code": "ISIN_MISSING"}]
    assert m["files"]["NIFTY 50"]["status"] == "OK" and m["files"]["NIFTY 50"]["reasons"] == []
    # independent datasetHash / policyHash recomputation (plan 4.6)
    cal_sha = hashlib.sha256(open(os.path.join(res.out_dir, "calendar.json"), "rb").read()).hexdigest()
    assert cal_sha == m["calendar"]["sha256"]
    lines = sorted("file:%s:%s" % (k, v["sha256"]) for k, v in m["files"].items()) + ["calendar:" + cal_sha, "policy:" + m["policyHash"], "contract:alphadesk-eod2/3"]
    assert hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest() == m["datasetHash"]
    pol = json.dumps({"adjustmentPolicy": "eod2-split-adjusted-price-only", "splits": [], "genuineMoves": [], "quarantine": {}, "gapPolicy": {"known_no_trade": {}, "gap_allow_all": {}}},
                     sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(pol).hexdigest() == m["policyHash"]
    assert ex.verify_snapshot(res.out_dir, m) == [] and ex.validate_manifest(m) == []
    on_disk = json.load(open(os.path.join(res.out_dir, "manifest.json"), encoding="utf-8"))
    assert on_disk == m and os.path.isfile(os.path.join(res.out_dir, "report.json"))


def test_warmup_rows_kept_and_history_start_is_a_hard_trim(tmp_path):
    dates = weekdays("2023-01-02", 900, {"2026-01-26"})
    w = World(tmp_path)
    w.dates, w.latest = dates, dates[-1]
    rows = {s: walk(dates, 100 + i) for i, s in enumerate(["aaa", "bbb", "ccc"])}
    idx = {"nifty 50": walk(dates, 20000, 0.0003, 0.004), "nifty bank": walk(dates, 45000, 0.0004, 0.006, 5)}
    reg = make_registry(default_start="2025-01-01")
    reg["instruments"][1]["historyStart"] = "2024-06-03"     # BBB: hard lower bound, no warm-up before it
    res, _ = build(tmp_path, w, reg, etf_rows=rows, idx_rows=idx)
    keep_from = (datetime.fromisoformat("2025-01-01") - timedelta(days=400)).date().isoformat()   # 2023-11-27
    assert res.files["AAA"]["first"] == next(d for d in dates if d >= keep_from) and res.files["AAA"]["first"] < "2025-01-01"
    assert res.files["BBB"]["first"] == "2024-06-03"
    assert res.files["AAA"]["firstObservedDate"] == dates[0]          # true first EOD2 date, before the kept warm-up
    assert res.files["NIFTY 50"]["first"] == res.files["AAA"]["first"]


# ── per-instrument failures never block publication ─────────────────────────────────────────────────────────────────────
def test_etf_missing_a_midlife_session_is_excluded_but_snapshot_stays_publishable(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    missing = w.dates[200]
    rows["bbb"] = [r for r in rows["bbb"] if r[0] != missing]
    res, _ = build(tmp_path, w, etf_rows=rows)
    assert res.publish_status == "PUBLISHABLE" and "BBB" not in res.files and set(res.files) == {"AAA", "CCC", "NIFTY 50", "NIFTY BANK"}
    x = res.excluded["BBB"]
    assert x["code"] == "MISSING_EXPECTED_SESSION" and missing in x["detail"]
    assert res.manifest["certification"] == "PARTIAL" and res.manifest["summary"]["excluded"] == 1 and res.manifest["summary"]["requested"] == 5
    assert ex.verify_snapshot(res.out_dir, res.manifest) == []


def test_one_index_broken_is_excluded_and_snapshot_stays_publishable(tmp_path):
    w = World(tmp_path)
    idx = copy.deepcopy(w.idx_rows)
    idx["nifty bank"][50] = idx["nifty bank"][50][:4] + ("Infinity",) + idx["nifty bank"][50][5:]
    res, _ = build(tmp_path, w, idx_rows=idx)
    assert res.publish_status == "PUBLISHABLE" and "NIFTY BANK" not in res.files and "NIFTY 50" in res.files
    assert res.excluded["NIFTY BANK"]["code"] == "NONFINITE_VALUE"
    assert set(res.files) >= {"AAA", "BBB", "CCC", "NIFTY 50"}


def test_all_indices_broken_means_no_calendar_and_unpublishable(tmp_path):
    w = World(tmp_path)
    idx = {k: [(r[0], None, None, None, None, None) for r in v] for k, v in w.idx_rows.items()}
    res, _ = build(tmp_path, w, idx_rows=idx)
    assert res.publish_status == "UNPUBLISHABLE" and res.manifest is None and "calendar" in res.message
    assert not os.path.exists(os.path.join(str(tmp_path), "out")) or not os.listdir(os.path.join(str(tmp_path), "out"))


def test_every_etf_broken_still_publishes_the_indices(tmp_path):
    reg = make_registry(etfs=("AAA", "BBB", "CCC"), quarantine={"AAA": "x", "BBB": "y", "CCC": "z"})
    res, _ = build(tmp_path, reg=reg)
    assert res.publish_status == "PUBLISHABLE" and set(res.files) == {"NIFTY 50", "NIFTY BANK"} and len(res.quarantined) == 3


def test_bad_date_and_nonfinite_values_get_their_codes(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["aaa"][10] = ("2025-13-45",) + rows["aaa"][10][1:]
    rows["bbb"][20] = rows["bbb"][20][:4] + ("NaN",) + rows["bbb"][20][5:]
    rows["ccc"][30] = rows["ccc"][30][:3] + ("Infinity",) + rows["ccc"][30][4:]
    res, _ = build(tmp_path, w, etf_rows=rows)
    assert res.excluded["AAA"]["code"] == "INVALID_DATE"
    assert res.excluded["BBB"]["code"] == "NONFINITE_VALUE" and res.excluded["CCC"]["code"] == "NONFINITE_VALUE"
    assert res.publish_status == "PUBLISHABLE"


def test_string_junk_in_a_numeric_cell_is_nonfinite_not_silently_dropped(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["aaa"][40] = rows["aaa"][40][:5] + ("12abc",)
    res, _ = build(tmp_path, w, etf_rows=rows)
    assert res.excluded["AAA"]["code"] == "NONFINITE_VALUE"


def test_close_only_index_fails_when_registry_requires_ohc(tmp_path):
    w = World(tmp_path)
    idx = copy.deepcopy(w.idx_rows)
    idx["nifty bank"] = [(r[0], None, None, None, r[4], None) for r in idx["nifty bank"]]
    res, _ = build(tmp_path, w, idx_rows=idx)
    assert res.excluded["NIFTY BANK"]["code"] == "MISSING_REQUIRED_FIELD" and res.publish_status == "PUBLISHABLE"
    reg = make_registry()
    reg["indices"][1]["requiredFields"] = ["close"]
    res2, _ = build(tmp_path, w, reg, out="out2", idx_rows=idx)
    assert "NIFTY BANK" in res2.files and res2.files["NIFTY BANK"]["status"] == "OK"


def test_older_close_only_history_is_trimmed_and_later_gaps_are_errors(tmp_path):
    w = World(tmp_path)
    idx = copy.deepcopy(w.idx_rows)
    idx["nifty bank"] = [((r[0], None, None, None, r[4], None) if i < 30 else r) for i, r in enumerate(idx["nifty bank"])]
    res, _ = build(tmp_path, w, idx_rows=idx)
    assert res.files["NIFTY BANK"]["first"] == w.dates[30] and res.files["NIFTY BANK"]["firstObservedDate"] == w.dates[30]
    idx2 = copy.deepcopy(w.idx_rows)
    idx2["nifty bank"][100] = (idx2["nifty bank"][100][0], None, None, None, idx2["nifty bank"][100][4], None)
    res2, _ = build(tmp_path, w, out="out2", idx_rows=idx2)
    assert res2.excluded["NIFTY BANK"]["code"] == "MISSING_REQUIRED_FIELD"


# ── anomalies and per-date approvals ───────────────────────────────────────────────────────────────────────────────────
def anomaly_world(tmp_path, at=(200,), factor=1.2):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    for i in at:
        rows["aaa"] = jump(rows["aaa"], i, factor)
    return w, rows


def test_plus_20_percent_with_approval_for_the_previous_day_only_is_quarantined(tmp_path):
    w, rows = anomaly_world(tmp_path)
    D = w.dates[200]
    reg = make_registry(moves=[dataops_move("AAA", w.dates[199])])
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    q = res.quarantined["AAA"]
    assert q["code"] == "ANOMALOUS_MOVE_UNRESOLVED" and q["events"][0]["date"] == D and q["events"][0]["ret"] == pytest.approx(0.2, abs=0.01)
    assert set(q["events"][0]) == {"date", "prevClose", "close", "ret"}
    assert "AAA" not in res.files and res.publish_status == "PUBLISHABLE" and res.manifest["certification"] == "PARTIAL"


def test_approval_for_the_exact_date_makes_it_ok_and_prices_are_unchanged(tmp_path):
    w, rows = anomaly_world(tmp_path)
    D = w.dates[200]
    reg = make_registry(moves=[dataops_move("AAA", D)])
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    assert "AAA" in res.files and "AAA" not in res.quarantined
    assert [r["code"] for r in res.files["AAA"]["reasons"]] == ["ISIN_MISSING"]        # real evidence -> no APPROVAL_EVIDENCE_MISSING
    out = {b[0]: b for b in bars_of(res, "AAA")}
    raw = {r[0]: r for r in rows["aaa"]}
    assert out[D][4] == raw[D][4] and out[D][1] == raw[D][1] and out[w.dates[199]][4] == raw[w.dates[199]][4]    # never auto-adjusted


def test_migrated_approval_is_ok_with_a_warning_that_names_its_provenance(tmp_path):
    w, rows = anomaly_world(tmp_path)
    D = w.dates[200]
    reg = make_registry(moves=[migrated_move("AAA", D)])
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    assert res.files["AAA"]["status"] == "WARN"
    w_ = [r for r in res.files["AAA"]["reasons"] if r["code"] == "APPROVAL_EVIDENCE_MISSING"]
    assert w_ == [{"code": "APPROVAL_EVIDENCE_MISSING", "date": D, "provenance": "migrated-v2-2026-09-11"}]


def test_a_new_anomaly_date_is_never_covered_by_an_older_approval(tmp_path):
    w, rows = anomaly_world(tmp_path, at=(150, 220))
    reg = make_registry(moves=[dataops_move("AAA", w.dates[150])])
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    assert [e["date"] for e in res.quarantined["AAA"]["events"]] == [w.dates[220]]


def test_unresolved_move_before_the_window_only_truncates_the_warmup(tmp_path):
    w = World(tmp_path)
    reg = make_registry(default_start="2025-06-02")
    rows = copy.deepcopy(w.etf_rows)
    D = w.dates[30]                         # 2025-02-11: inside the warm-up range, before the window start
    rows["aaa"] = jump(rows["aaa"], 30, 0.1)
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    assert "AAA" in res.files and "AAA" not in res.quarantined
    assert res.files["AAA"]["first"] == D and res.files["AAA"]["warmupTruncatedFrom"] == D


def test_indices_are_not_subject_to_the_anomaly_gate(tmp_path):
    w = World(tmp_path)
    idx = copy.deepcopy(w.idx_rows)
    idx["nifty 50"] = jump(idx["nifty 50"], 250, 0.8)
    res, _ = build(tmp_path, w, idx_rows=idx)
    assert "NIFTY 50" in res.files


# ── corporate actions ───────────────────────────────────────────────────────────────────────────────────────────────────
def split_rows(rows, i, ratio):
    """Unadjusted split: prices before rows[i] are `ratio` times too high, volume too low."""
    out = []
    for j, r in enumerate(rows):
        if j < i:
            out.append((r[0], round(r[1] * ratio, 4), round(r[2] * ratio, 4), round(r[3] * ratio, 4), round(r[4] * ratio, 4), int(r[5] / ratio)))
        else:
            out.append(r)
    return out


def test_declared_split_is_applied_like_v2(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    i = 180
    rows["aaa"] = split_rows(rows["aaa"], i, 10)
    S = w.dates[i]
    split = {"symbol": "AAA", "date": S, "ratioNum": 10, "ratioDen": 1, "evidence": "NSE", "approvedAt": "2026-03-01T10:00:00+05:30", "provenance": "dataops"}
    res, _ = build(tmp_path, w, make_registry(splits=[split]), etf_rows=rows)
    assert "AAA" in res.files
    out = {b[0]: b for b in bars_of(res, "AAA")}
    orig = {r[0]: r for r in w.etf_rows["aaa"]}
    for d in (w.dates[100], w.dates[i - 1], S):
        assert out[d][4] == pytest.approx(orig[d][4], abs=1e-3)
    assert out[w.dates[i - 1]][5] == pytest.approx(orig[w.dates[i - 1]][5], rel=0.001)   # volume multiplied back


def test_declared_split_that_disagrees_with_the_data_is_unresolved(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["aaa"] = split_rows(rows["aaa"], 180, 10)
    split = {"symbol": "AAA", "date": w.dates[180], "ratioNum": 5, "ratioDen": 1, "evidence": "x", "approvedAt": "2026-03-01T10:00:00+05:30", "provenance": "dataops"}
    res, _ = build(tmp_path, w, make_registry(splits=[split]), etf_rows=rows)
    q = res.quarantined["AAA"]
    assert q["code"] == "CORPORATE_ACTION_UNRESOLVED" and "declared split 5:1 but implied" in json.dumps(q)
    split2 = dict(split, date="2027-01-04", ratioNum=10)
    res2, _ = build(tmp_path, w, make_registry(splits=[split2]), out="out2")
    assert res2.quarantined["AAA"]["code"] == "CORPORATE_ACTION_UNRESOLVED"       # date beyond the data -> cannot verify


def test_registry_quarantine_is_honoured(tmp_path):
    res, _ = build(tmp_path, reg=make_registry(quarantine={"BBB": "operator hold"}))
    assert res.quarantined["BBB"]["code"] == "EXPORT_FAILED" and "REGISTRY_QUARANTINE: operator hold" in json.dumps(res.quarantined["BBB"]) and "BBB" not in res.files


# ── OHLC ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_small_ohlc_discrepancy_is_a_warning_and_large_is_a_block(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    r = rows["aaa"][60]
    rows["aaa"][60] = (r[0], r[1], round(max(r[1], r[4]) * 0.9997, 4), r[3], r[4], r[5])       # high 0.03% below max(open, close)
    r = rows["bbb"][60]
    rows["bbb"][60] = (r[0], r[1], round(max(r[1], r[4]) * 0.99, 4), r[3], r[4], r[5])        # 1% -> block
    res, _ = build(tmp_path, w, etf_rows=rows)
    assert res.files["AAA"]["status"] == "WARN" and "SMALL_OHLC_DISCREPANCY" in [x["code"] for x in res.files["AAA"]["reasons"]]
    assert res.excluded["BBB"]["code"] == "OHLC_INCONSISTENT"


# ── identity ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_two_candidate_files_are_ambiguous(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["oldaaa"] = copy.deepcopy(rows["aaa"])
    reg = make_registry()
    reg["instruments"][0]["aliases"] = [{"symbol": "OLDAAA", "from": "2020-01-01", "to": "2025-01-01"}]
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    assert res.excluded["AAA"]["code"] == "AMBIGUOUS_SYMBOL" and "aaa.csv" in res.excluded["AAA"]["detail"] and "oldaaa.csv" in res.excluded["AAA"]["detail"]


def test_renamed_symbol_resolves_through_its_alias(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["oldaaa"] = rows.pop("aaa")
    reg = make_registry()
    reg["instruments"][0]["aliases"] = [{"symbol": "OLDAAA", "from": "2020-01-01", "to": "2025-01-01"}]
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    assert "AAA" in res.files


def test_symbol_not_in_eod2_is_excluded(tmp_path):
    reg = make_registry(etfs=("AAA", "BBB", "CCC", "ZZZ"))
    res, _ = build(tmp_path, reg=reg)
    assert res.excluded["ZZZ"]["code"] == "NOT_IN_EOD2" and res.publish_status == "PUBLISHABLE" and res.manifest["summary"]["requested"] == 6


def test_isin_verification_and_series_check(tmp_path):
    reg = make_registry()
    reg["instruments"][0]["isin"] = "INF204KB14I2"
    reg["instruments"][1]["isin"] = "INF204KB15I9"
    reg["instruments"][2]["isin"] = ""
    res, _ = build(tmp_path, reg=reg, isin={"AAA": "INF204KB14I2", "BBB": "INF999999999"})
    assert res.files["AAA"]["status"] == "OK" and res.files["AAA"]["isin"] == "INF204KB14I2" and res.files["AAA"]["reasons"] == []
    assert res.excluded["BBB"]["code"] == "IDENTITY_MISMATCH" and "INF204KB15I9" in res.excluded["BBB"]["detail"]
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["ccc"] = [r + ("SM",) for r in rows["ccc"]]
    res2, _ = build(tmp_path, w, out="out2", etf_rows=rows)
    assert res2.excluded["CCC"]["code"] == "IDENTITY_MISMATCH" and "SERIES" in res2.excluded["CCC"]["detail"]


# ── listing dates: never derived from the first bar ─────────────────────────────────────────────────────────────────────
def test_truncated_history_without_a_listing_date_is_a_warning_never_silently_ok(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    first = next(i for i, r in enumerate(rows["aaa"]) if r[0] >= "2025-09-01")
    rows["aaa"] = rows["aaa"][first:]
    res, _ = build(tmp_path, w, etf_rows=rows)
    f = res.files["AAA"]
    assert f["status"] == "WARN" and f["listingDate"] is None and f["listingDateSource"] == "none" and f["firstObservedDate"] == rows["aaa"][0][0]
    assert {"code": "LISTING_DATE_UNVERIFIED", "firstObservedDate": rows["aaa"][0][0]} in f["reasons"]
    assert "AAA" in res.warnings_listing


def test_verified_listing_date_replaces_the_warning_and_sets_the_coverage_start(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    first = next(i for i, r in enumerate(rows["aaa"]) if r[0] >= "2025-09-01")
    rows["aaa"] = rows["aaa"][first:]
    reg = make_registry()
    reg["instruments"][0].update(listingDate=rows["aaa"][0][0], listingDateEvidence="NSE circular")
    res, _ = build(tmp_path, w, reg, etf_rows=rows)
    codes = [r["code"] for r in res.files["AAA"]["reasons"]]
    assert "LISTING_DATE_UNVERIFIED" not in codes and "NOT_YET_LISTED" in codes and res.files["AAA"]["listingDateSource"] == "registry-evidence"
    reg["instruments"][0]["listingDate"] = "2025-06-02"          # claims it listed earlier than its first bar -> sessions missing
    res2, _ = build(tmp_path, w, reg, out="out2", etf_rows=rows)
    assert res2.excluded["AAA"]["code"] == "MISSING_EXPECTED_SESSION"


# ── staleness, latest session, gap policy ──────────────────────────────────────────────────────────────────────────────
def test_stale_etf_and_lagging_index_are_instrument_level_only(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    rows["ccc"] = rows["ccc"][:-3]
    idx = copy.deepcopy(w.idx_rows)
    idx["nifty it"] = walk(w.dates, 30000, 0.0003, 0.005, 2)
    idx["nifty bank"] = idx["nifty bank"][:-1]                                  # one index lags by a session; the other two agree
    reg = make_registry(indices=("NIFTY 50", "NIFTY BANK", "NIFTY IT"))
    res, _ = build(tmp_path, w, reg, etf_rows=rows, idx_rows=idx)
    assert res.publish_status == "PUBLISHABLE" and res.manifest["latestCompletedSession"] == w.latest
    assert res.excluded["CCC"]["code"] == "STALE_DATASET" and res.excluded["NIFTY BANK"]["code"] == "STALE_DATASET"
    assert {"AAA", "BBB", "NIFTY 50", "NIFTY IT"} <= set(res.files)


def test_gap_policy_known_no_trade_and_allow_all(tmp_path):
    w = World(tmp_path)
    rows = copy.deepcopy(w.etf_rows)
    m1, m2 = w.dates[150], w.dates[151]
    rows["bbb"] = [r for r in rows["bbb"] if r[0] != m1]
    rows["ccc"] = [r for r in rows["ccc"] if r[0] not in (m1, m2)]
    gap = {"known_no_trade": {"BBB": [m1]}, "gap_allow_all": {"CCC": "illiquid"}}
    res, _ = build(tmp_path, w, make_registry(gap=gap), etf_rows=rows)
    assert res.files["BBB"]["noTradeDays"] == [m1] and res.files["CCC"]["noTradeDays"] == [m1, m2]
    gap2 = {"known_no_trade": {"BBB": [m2]}, "gap_allow_all": {}}            # allows the wrong date
    res2, _ = build(tmp_path, w, make_registry(gap=gap2), out="out2", etf_rows=rows)
    assert res2.excluded["BBB"]["code"] == "MISSING_EXPECTED_SESSION"


# ── calendar ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_calendar_extends_to_coverage_to_and_flags_special_sessions(tmp_path):
    w = World(tmp_path)
    sat, muhurat = "2025-11-01", "2025-10-21"           # a Saturday; a weekday listed as a Laxmi Pujan holiday
    etf = copy.deepcopy(w.etf_rows)
    idx = copy.deepcopy(w.idx_rows)
    for store in (etf, idx):
        for k, rows in store.items():
            prev = {r[0]: r for r in rows}
            extra = [(sat, prev["2025-10-31"][4], prev["2025-10-31"][4] * 1.001, prev["2025-10-31"][4] * 0.999, prev["2025-10-31"][4], 1000)]
            rows[:] = sorted(rows + extra)
    root = make_eod2(tmp_path, etf, idx, holidays={"21-Oct-2025": "Diwali Laxmi Pujan*", "26-Jan-2026": "Republic Day", "14-Sep-2026": "Ganesh Chaturthi"},
                     year=2026, special_txt=[])
    cfg = write_registry(tmp_path, make_registry())
    res = ex.build_snapshot(root, cfg, os.path.join(str(tmp_path), "o"), now=NOW, existing_roots=[])
    cal = json.load(open(os.path.join(res.out_dir, "calendar.json"), encoding="utf-8"))
    assert cal["id"] == "nse-eq-" + w.latest and cal["timezone"] == "Asia/Kolkata" and cal["cutoffIST"] == "19:15"
    assert cal["coverageFrom"] == "2025-01-01" and cal["coverageTo"] == "2026-12-31" and cal["verifiedThrough"] == "2026-12-31"
    assert cal["latestCompletedSession"] == w.latest and cal["sessions"] == sorted(set(cal["sessions"]))
    assert cal["sessions"][-1] == "2026-12-31" and "2026-09-14" not in cal["sessions"] and "2026-12-25" in cal["sessions"]
    future = [d for d in cal["sessions"] if d > w.latest]
    assert future[0] == (datetime.fromisoformat(w.latest) + timedelta(days=1 if datetime.fromisoformat(w.latest).weekday() < 4 else 3)).date().isoformat()
    assert {"date": sat, "kind": "SPECIAL"} in cal["specialSessions"] and sat not in cal["sessions"]
    assert {"date": muhurat, "kind": "MUHURAT"} in cal["specialSessions"] and muhurat not in cal["sessions"]
    assert "2026-01-26" in cal["holidays"] and res.manifest["holidays"] == cal["holidays"]
    assert res.manifest["calendar"]["verifiedThrough"] == "2026-12-31" and res.publish_status == "PUBLISHABLE"
    # ETFs are not required to have the special-session bar and here they do: it stays a valid bar
    assert sat in [b[0] for b in bars_of(res, "AAA")]


def test_announced_special_session_on_a_weekday_is_special(tmp_path):
    w = World(tmp_path)
    d = "2025-09-03"
    root = make_eod2(tmp_path, copy.deepcopy(w.etf_rows), copy.deepcopy(w.idx_rows), special_txt=[d])
    res = ex.build_snapshot(root, write_registry(tmp_path, make_registry()), os.path.join(str(tmp_path), "o"), now=NOW, existing_roots=[])
    cal = json.load(open(os.path.join(res.out_dir, "calendar.json"), encoding="utf-8"))
    assert {"date": d, "kind": "SPECIAL"} in cal["specialSessions"] and d not in cal["sessions"]


def test_missing_holiday_list_limits_verified_through(tmp_path):
    w = World(tmp_path)
    root = make_eod2(tmp_path, copy.deepcopy(w.etf_rows), copy.deepcopy(w.idx_rows), holidays={}, year=2025)
    res = ex.build_snapshot(root, write_registry(tmp_path, make_registry()), os.path.join(str(tmp_path), "o"), now=NOW, existing_roots=[])
    m = res.manifest["calendar"]
    assert m["verifiedThrough"] == w.latest and m["coverageTo"] == "2026-12-31"


# ── determinism, ids, self-verification ────────────────────────────────────────────────────────────────────────────────
def test_identical_runs_are_byte_identical(tmp_path):
    w = World(tmp_path)
    r1, _ = build(tmp_path, w, out="run1")
    r2, _ = build(tmp_path, w, out="run2")
    assert r1.manifest["datasetHash"] == r2.manifest["datasetHash"] and r1.manifest["policyHash"] == r2.manifest["policyHash"]
    names = sorted(os.listdir(r1.out_dir))
    for sub in ("etf", "index"):
        assert sorted(os.listdir(os.path.join(r1.out_dir, sub))) == sorted(os.listdir(os.path.join(r2.out_dir, sub)))
    for root, _, files in os.walk(r1.out_dir):
        for fn in files:
            a = os.path.join(root, fn)
            b = os.path.join(r2.out_dir, os.path.relpath(a, r1.out_dir))
            assert open(a, "rb").read() == open(b, "rb").read(), a
    assert names == sorted(os.listdir(r2.out_dir))


def test_dataset_id_is_one_plus_the_highest_existing_revision_across_roots(tmp_path):
    w = World(tmp_path)
    out = os.path.join(str(tmp_path), "work")
    snaps = os.path.join(str(tmp_path), "snapshots")
    r1, _ = build(tmp_path, w, out="work", existing_roots=[out, snaps])
    assert r1.dataset_id == w.latest + "-r1"
    os.makedirs(os.path.join(snaps, w.latest + "-r4"))
    os.makedirs(os.path.join(snaps, "2020-01-01-r9"))                       # other dates do not count
    r2, _ = build(tmp_path, w, out="work", existing_roots=[out, snaps])
    assert r2.dataset_id == w.latest + "-r5"
    assert ex.next_dataset_id("2030-01-01", [out, snaps]) == "2030-01-01-r1"


def test_self_verify_detects_tampering_and_unpublishable_is_recorded(tmp_path, monkeypatch):
    res, _ = build(tmp_path)
    path = os.path.join(res.out_dir, res.files["AAA"]["path"])
    good = open(path, "rb").read()
    open(path, "wb").write(good.replace(b"0", b"1", 1))
    assert any(p.startswith("HASH_MISMATCH AAA") for p in ex.verify_snapshot(res.out_dir, res.manifest))
    open(path, "wb").write(good)
    assert ex.verify_snapshot(res.out_dir, res.manifest) == []
    bad = copy.deepcopy(res.manifest)
    bad["datasetHash"] = "0" * 64
    assert "datasetHash mismatch" in ex.verify_snapshot(res.out_dir, bad)
    bad2 = copy.deepcopy(res.manifest)
    del bad2["files"]["AAA"]["sha256"]
    assert ex.validate_manifest(bad2)
    monkeypatch.setattr(ex, "verify_snapshot", lambda out_dir, manifest: ["HASH_MISMATCH AAA"])
    res2, _ = build(tmp_path, out="out2")
    assert res2.publish_status == "UNPUBLISHABLE" and "HASH_MISMATCH AAA" in res2.message
    assert json.load(open(os.path.join(res2.out_dir, "manifest.json"), encoding="utf-8"))["publishStatus"] == "UNPUBLISHABLE"


def test_manifest_schema_validator_rejects_bad_documents(tmp_path):
    res, _ = build(tmp_path)
    for mutate in (lambda m: m.pop("policyHash"), lambda m: m.update(contract="x"), lambda m: m.update(publishStatus="MAYBE"), lambda m: m.update(fallbackUsed=True),
                   lambda m: m["summary"].update(ok=99), lambda m: m.update(snapshotBase="x/"), lambda m: m["calendar"].pop("sha256"),
                   lambda m: m["files"]["AAA"].update(status="BROKEN"), lambda m: m.update(datasetHash="zz")):
        m = copy.deepcopy(res.manifest)
        mutate(m)
        assert ex.validate_manifest(m), "validator accepted a bad manifest"


def test_export_never_writes_into_the_eod2_folder(tmp_path):
    w = World(tmp_path)
    root = w.build()
    before = {os.path.join(d, f): (os.path.getmtime(os.path.join(d, f)), os.path.getsize(os.path.join(d, f))) for d, _, fs in os.walk(root) for f in fs}
    ex.build_snapshot(root, write_registry(tmp_path, make_registry()), os.path.join(str(tmp_path), "o"), now=NOW, existing_roots=[])
    after = {os.path.join(d, f): (os.path.getmtime(os.path.join(d, f)), os.path.getsize(os.path.join(d, f))) for d, _, fs in os.walk(root) for f in fs}
    assert before == after


def test_exporter_on_the_shared_eod2_min_fixture():
    root = r"C:\dev\fixtures\eod2_min"
    if not os.path.isdir(root):
        pytest.skip("fixtures missing")
    import tempfile
    reg = make_registry(etfs=("NIFTYBEES", "GOLDBEES", "LIQUIDBEES"), indices=("NIFTY 50",), default_start="2026-02-02")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = write_registry(tmp, reg)
        res = ex.build_snapshot(root, cfg, os.path.join(tmp, "o"), now=NOW, existing_roots=[])
        assert res.publish_status == "PUBLISHABLE" and set(res.files) == {"NIFTYBEES", "GOLDBEES", "LIQUIDBEES", "NIFTY 50"}, (res.message, res.excluded, res.quarantined)
        assert res.manifest["summary"]["requested"] == 4 and all(f["bars"] == 30 for f in res.files.values())
        assert res.manifest["latestCompletedSession"] == res.files["NIFTYBEES"]["last"]


def test_reasons_table_matches_the_plan_verbatim():
    plan = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CONTRACT.md"), encoding="utf-8").read()
    sect = plan[plan.index("### 4.4 Reason codes"):plan.index("### 4.5")]
    rows = {}
    for ln in sect.splitlines():
        cells = [c.strip().replace("\x00", "|") for c in ln.strip().replace("\\|", "\x00").strip("|").split("|")]
        if len(cells) == 4 and cells[0].isupper() and cells[0] not in ("CODE",) and " " not in cells[0]:
            rows[cells[0]] = cells
    assert set(rows) == set(R.REASONS), set(rows) ^ set(R.REASONS)
    for code, (_, lvl, meaning, elig) in rows.items():
        want_level = "ok" if lvl == "ok" else lvl
        assert R.REASONS[code][0] == want_level, code
        e = elig.lower()
        want = "conditional" if "only if" in e else ("yes" if e.startswith("yes") else "no")
        assert R.REASONS[code][1] == want, (code, elig)
    assert len(R.REASONS) == len(rows) == 31
