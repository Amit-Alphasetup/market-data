"""v4 export (Backtest Lab plan r2, P2 session A): stocks in the registry, hist/tail split, content-addressed names, hash chain, reviewed
actions, the stock gap gate, yearly rebase, names with & and spaces - and v3 staying exactly as it was."""
import copy
import json
import os

import pytest

from dataops import export as ex
from dataops import export_v4 as x4
from dataops import hashing as H
from dataops import schema_universe as su
from eod2_factory import World, make_registry, walk, weekdays, write_registry

from datetime import datetime, timezone
NOW = datetime(2026, 3, 27, 14, 11, 7, tzinfo=timezone.utc)
APPROVED = "2026-11-13T10:00:00+05:30"


def eq(sym, isin=""):
    return {"id": "NSE:" + sym, "symbol": sym, "kind": "EQ", "exchange": "NSE", "isin": isin, "name": sym + " Ltd", "assetClass": "equity",
            "engines": [], "linkedIndex": None, "listingDate": None, "listingDateEvidence": None, "delistingDate": None, "aliases": [],
            "eod2File": None, "historyStart": None, "enabled": True}


def world(tmp_path, stocks, reg_edit=None, **kw):
    """World (3 ETFs, 2 indices, 320 weekdays from 2025-01-01 -> latest in 2026) + stock CSVs {SYMBOL: rows}."""
    w = World(tmp_path)
    rows = dict(w.etf_rows)
    rows.update({s.lower(): r for s, r in stocks.items()})
    root = w.build(etf_rows=rows, **kw)
    reg = make_registry()
    reg["eqHistoryFloor"] = "2025-01-01"
    reg["instruments"] += [eq(s) for s in stocks]
    if reg_edit:
        reg_edit(reg)
    return w, root, write_registry(tmp_path, reg)


def build(tmp_path, root, cfg, name="out", **kw):
    return x4.build_v4(root, cfg, os.path.join(str(tmp_path), name), now=NOW, **kw)


def load(res, rel):
    with open(os.path.join(res.out_dir, *rel.split("/")), "rb") as fh:
        return fh.read()


def joined(res, key):
    f = res.manifest["files"][key]
    bars = json.loads(load(res, f["hist"]["path"]))["bars"] if f["hist"] else []
    tail = json.loads(load(res, res.manifest["tails"][f["kind"]]["path"]))["series"].get(key, [])
    return bars + tail


def gap_rows(dates, at, factor, start=500.0):
    """A clean walk whose prices from index `at` on are multiplied by `factor` (an unadjusted corporate action on that date)."""
    rows = walk(dates, start, phase=7)
    return [r if i < at else (r[0],) + tuple(round(v * factor, 4) for v in r[1:5]) + (r[5],) for i, r in enumerate(rows)]


def test_hist_tail_split_content_addressed_names_and_self_verification(tmp_path):
    w, root, cfg = world(tmp_path, {"RELI": walk(World(tmp_path).dates, 1200, phase=2)})
    res = build(tmp_path, root, cfg)
    m = res.manifest
    assert res.publish_status == "PUBLISHABLE" and m["contract"] == "alphadesk-eod2/4" and m["histEnd"] == "2025-12-31"
    assert m["datasetId"] == w.latest + "-r1" and m["summary"]["byKind"]["EQ"]["published"] == 1
    f = m["files"]["RELI"]
    raw = load(res, f["hist"]["path"])
    sha = H.sha256_bytes(raw)
    assert f["hist"]["path"] == "hist/EQ/RELI.%s.json" % sha[:12] and f["hist"]["sha256"] == sha and f["hist"]["bytes"] == len(raw)
    doc = json.loads(raw)
    assert doc["kind"] == "EQ" and doc["fields"] == ex.FIELDS and doc["bars"][-1][0] <= "2025-12-31" == m["histEnd"]
    tail = json.loads(load(res, m["tails"]["EQ"]["path"]))
    assert m["tails"]["EQ"]["path"] == "tail/EQ.%s.json" % m["datasetId"] and tail["from"] == "2026-01-01" and tail["kind"] == "EQ"
    assert tail["series"]["RELI"][0][0] >= "2026-01-01" and tail["series"]["RELI"][-1][0] == w.latest
    assert f["bars"] == len(doc["bars"]) + len(tail["series"]["RELI"]) == len(w.dates) and f["tailBars"] == len(tail["series"]["RELI"])
    assert [b[0] for b in joined(res, "RELI")] == w.dates                      # nothing lost, nothing duplicated at the year boundary
    assert x4.verify_v4(res.out_dir, m) == []
    assert json.loads(load(res, "manifest.json")) == m
    # tamper -> the verifier says so
    p = os.path.join(res.out_dir, *f["hist"]["path"].split("/"))
    with open(p, "ab") as fh:
        fh.write(b" ")
    assert any("HASH_MISMATCH RELI" in x for x in x4.verify_v4(res.out_dir, m))


def test_etf_and_index_bars_equal_the_v3_snapshot_and_v3_ignores_stocks(tmp_path):
    w, root, cfg = world(tmp_path, {"RELI": walk(World(tmp_path).dates, 1200, phase=2)})
    v3 = ex.build_snapshot(root, cfg, os.path.join(str(tmp_path), "v3"), now=NOW, existing_roots=[])
    assert sorted(v3.files) == ["AAA", "BBB", "CCC", "NIFTY 50", "NIFTY BANK"] and v3.summary["requested"] == 5      # no stock in v3
    res = build(tmp_path, root, cfg)
    for key, f3 in v3.files.items():
        with open(os.path.join(v3.out_dir, f3["path"]), encoding="utf-8") as fh:
            assert joined(res, key) == json.load(fh)["bars"], key
    assert res.manifest["latestCompletedSession"] == v3.manifest["latestCompletedSession"]


def test_v3_output_is_byte_identical_with_and_without_stocks_in_the_registry(tmp_path):
    w = World(tmp_path)
    root = w.build(etf_rows={**w.etf_rows, "reli": walk(w.dates, 1200, phase=2)})
    reg = make_registry()
    a = ex.build_snapshot(root, write_registry(tmp_path, reg, "a.json"), os.path.join(str(tmp_path), "a"), now=NOW, existing_roots=[])
    reg2 = copy.deepcopy(reg)
    reg2["instruments"].append(eq("RELI"))
    b = ex.build_snapshot(root, write_registry(tmp_path, reg2, "b.json"), os.path.join(str(tmp_path), "b"), now=NOW, existing_roots=[])
    assert a.manifest["datasetHash"] == b.manifest["datasetHash"] and a.manifest["files"] == b.manifest["files"]


def test_symbol_listed_this_year_has_no_hist_file(tmp_path):
    w0 = World(tmp_path)
    new = [d for d in w0.dates if d >= "2026-02-02"]
    w, root, cfg = world(tmp_path, {"NEWCO": walk(new, 300, phase=4)})
    res = build(tmp_path, root, cfg)
    f = res.manifest["files"]["NEWCO"]
    assert f["hist"] is None and f["tailBars"] == len(new) == f["bars"] and f["first"] == "2026-02-02"
    assert not [n for n in os.listdir(os.path.join(res.out_dir, "hist", "EQ")) if n.startswith("NEWCO")]
    assert x4.verify_v4(res.out_dir, res.manifest) == []


def test_names_with_ampersand_and_spaces(tmp_path):
    w, root, cfg = world(tmp_path, {"M&M": walk(World(tmp_path).dates, 2800, phase=1), "M&MFIN": walk(World(tmp_path).dates, 280, phase=6)})
    res = build(tmp_path, root, cfg)
    m = res.manifest
    assert m["files"]["M&M"]["hist"]["path"].startswith("hist/EQ/M&M.") and m["files"]["NIFTY 50"]["hist"]["path"].startswith("hist/INDEX/NIFTY 50.")
    assert len(joined(res, "M&M")) == len(w.dates) and len(joined(res, "NIFTY 50")) > 0 and x4.verify_v4(res.out_dir, m) == []


def test_dataset_hash_chain(tmp_path):
    w, root, cfg = world(tmp_path, {"RELI": walk(World(tmp_path).dates, 1200, phase=2)})
    a, b = build(tmp_path, root, cfg, "a"), build(tmp_path, root, cfg, "b")
    assert a.manifest["datasetHash"] == b.manifest["datasetHash"]              # deterministic
    m = a.manifest
    lines = sorted("hist:%s:%s" % (k, f["hist"]["sha256"]) for k, f in m["files"].items()) + sorted("tail:%s:%s" % (k, t["sha256"]) for k, t in m["tails"].items())
    lines += ["calendar:" + m["calendar"]["sha256"], "policy:" + m["policyHash"], "contract:alphadesk-eod2/4"]
    assert m["datasetHash"] == H.sha256_bytes(("\n".join(lines) + "\n").encode())
    # one changed close in the tail year changes the tail hash and the dataset hash, not that symbol's hist file
    rows = walk(World(tmp_path).dates, 1200, phase=2)
    r = list(rows[-3]); r[4] = round(r[4] * 1.001, 4); r[2] = max(r[2], r[4]); rows[-3] = tuple(r)
    w2, root2, cfg2 = world(os.path.join(str(tmp_path), "w2"), {"RELI": rows})
    c = x4.build_v4(root2, cfg2, os.path.join(str(tmp_path), "c"), now=NOW)
    assert c.manifest["files"]["RELI"]["hist"]["sha256"] == m["files"]["RELI"]["hist"]["sha256"]
    assert c.manifest["tails"]["EQ"]["sha256"] != m["tails"]["EQ"]["sha256"] and c.manifest["datasetHash"] != m["datasetHash"]
    assert c.manifest["tails"]["ETF"]["sha256"].replace(c.manifest["datasetId"], "") == m["tails"]["ETF"]["sha256"].replace(m["datasetId"], "")


def test_unapproved_overnight_gap_truncates_the_stock_history_and_is_listed(tmp_path):
    dates = World(tmp_path).dates
    w, root, cfg = world(tmp_path, {"DMRG": gap_rows(dates, 120, 0.6), "CALM": walk(dates, 90, phase=3)})
    res = build(tmp_path, root, cfg)
    f = res.manifest["files"]["DMRG"]
    assert f["first"] == dates[120] == f["historyTruncatedFrom"] and f["bars"] == len(dates) - 120 and [r["code"] for r in f["reasons"]] == ["ISIN_MISSING"]   # truncation is not a block
    ev = res.manifest["pendingActions"]["DMRG"]
    assert len(ev) == 1 and ev[0]["date"] == dates[120] and abs(ev[0]["gap"] + 0.4) < 0.02 and abs(ev[0]["suggestedFactor"] - 0.6) < 0.02
    assert "historyTruncatedFrom" not in res.manifest["files"]["CALM"] and "CALM" not in res.manifest["pendingActions"]
    assert res.manifest["summary"]["historyTruncated"] == 1
    prop = x4.propose_actions(root, cfg)
    assert [(p[0], p[1], p[6]) for p in prop] == [("DMRG", dates[120], True)]


def test_big_move_made_by_trading_during_the_day_is_kept(tmp_path):
    dates = World(tmp_path).dates
    rows = walk(dates, 400, phase=5)
    o = rows[150][1]
    crash = (rows[150][0], o, round(o * 1.002, 4), round(o * 0.70, 4), round(o * 0.72, 4), rows[150][5])     # opens flat, closes -28%
    rows = rows[:150] + [crash] + [(r[0],) + tuple(round(v * 0.72, 4) for v in r[1:5]) + (r[5],) for r in rows[151:]]
    w, root, cfg = world(tmp_path, {"CRSH": rows})
    res = build(tmp_path, root, cfg)
    f = res.manifest["files"]["CRSH"]
    assert f["bars"] == len(dates) and "historyTruncatedFrom" not in f and res.manifest["pendingActions"] == {}


def test_approved_action_factor_adjusts_prices_and_volume_before_the_date(tmp_path):
    dates = World(tmp_path).dates
    raw = gap_rows(dates, 120, 0.6)

    def approve(reg):
        reg["actions"] = [{"symbol": "DMRG", "date": dates[120], "type": "demerger", "factor": 0.6, "evidence": "NSE circular 123/2025 (test)",
                           "approvedAt": APPROVED, "provenance": "dataops"}]
    w, root, cfg = world(tmp_path, {"DMRG": raw}, reg_edit=approve)
    res = build(tmp_path, root, cfg)
    f = res.manifest["files"]["DMRG"]
    assert f["bars"] == len(dates) and "historyTruncatedFrom" not in f and res.manifest["pendingActions"] == {}
    assert f["adjustments"] == [{"date": dates[120], "type": "demerger", "factor": 0.6}]
    bars = joined(res, "DMRG")
    for i in (0, 60, 119):
        for k in (1, 2, 3, 4):
            assert bars[i][k] == round(raw[i][k] * 0.6, 4)
        assert bars[i][5] == int(round(raw[i][5] / 0.6))
    assert bars[120][1:] == [raw[120][1], raw[120][2], raw[120][3], raw[120][4], raw[120][5]]
    assert abs(bars[120][1] / bars[119][4] - 1) < 0.02                         # the series is continuous across the action date


def test_actions_need_evidence_and_approval(tmp_path):
    reg = make_registry()
    reg["instruments"].append(eq("DMRG"))
    base = {"symbol": "DMRG", "date": "2025-06-17", "type": "demerger", "factor": 0.6, "evidence": "NSE circular", "approvedAt": APPROVED, "provenance": "dataops"}
    reg["actions"] = [base]
    assert su.validate(reg) == []
    for edit, word in (({"evidence": None}, "evidence"), ({"approvedAt": None}, "approvedAt"), ({"factor": 0}, "factor"), ({"type": "magic"}, "type"),
                       ({"symbol": "NOPE"}, "symbol")):
        reg["actions"] = [{**base, **edit}]
        assert any(word in e for e in su.validate(reg)), word
    reg["actions"] = [base, dict(base)]
    assert any("duplicate action" in e for e in su.validate(reg))


def test_genuine_move_approval_keeps_the_gap_without_adjusting(tmp_path):
    dates = World(tmp_path).dates
    raw = gap_rows(dates, 120, 0.6)

    def approve(reg):
        reg["genuineMoves"].append({"symbol": "DMRG", "date": dates[120], "evidence": "exchange filing (test)", "approvedAt": APPROVED, "provenance": "dataops"})
    w, root, cfg = world(tmp_path, {"DMRG": raw}, reg_edit=approve)
    res = build(tmp_path, root, cfg)
    bars = joined(res, "DMRG")
    assert len(bars) == len(dates) and bars[0][4] == raw[0][4] and res.manifest["pendingActions"] == {}


def test_registry_quarantine_and_stale_stock_stay_out_of_files(tmp_path):
    dates = World(tmp_path).dates

    def q(reg):
        reg["quarantine"] = {"BADCO": "under review"}
    w, root, cfg = world(tmp_path, {"BADCO": walk(dates, 50, phase=1), "GONE": walk(dates[:200], 70, phase=2), "FINE": walk(dates, 60, phase=3)}, reg_edit=q)
    res = build(tmp_path, root, cfg)
    m = res.manifest
    assert "BADCO" in m["quarantined"] and m["quarantined"]["BADCO"]["kind"] == "EQ" and "BADCO" not in m["files"]
    assert m["excluded"]["GONE"]["code"] == "STALE_DATASET" and "FINE" in m["files"]
    assert m["certification"] == "PARTIAL" and res.publish_status == "PUBLISHABLE"                      # one broken stock never blocks the rest
    assert m["summary"]["byKind"]["EQ"] == {"published": 1, "excluded": 1, "quarantined": 1}
    tail = json.loads(load(res, m["tails"]["EQ"]["path"]))
    assert sorted(tail["series"]) == ["FINE"]


def test_stock_session_without_a_bar_is_a_no_trade_day_not_a_block(tmp_path):
    dates = World(tmp_path).dates
    rows = [r for r in walk(dates, 80, phase=2) if r[0] not in (dates[200], dates[201])]
    w, root, cfg = world(tmp_path, {"HALT": rows})
    res = build(tmp_path, root, cfg)
    assert res.manifest["files"]["HALT"]["noTradeDays"] == [dates[200], dates[201]] and res.manifest["files"]["HALT"]["bars"] == len(dates) - 2


def test_floor_drops_older_stock_bars(tmp_path):
    dates = World(tmp_path).dates

    def floor(reg):
        reg["eqHistoryFloor"] = dates[100]
    w, root, cfg = world(tmp_path, {"OLD": walk(dates, 80, phase=2)}, reg_edit=floor)
    res = build(tmp_path, root, cfg)
    assert res.manifest["files"]["OLD"]["first"] == dates[100] and res.manifest["eqHistoryFloor"] == dates[100]
    # a broken bar BEFORE the floor (real case: one zero-price row in 1996-2001) does not exclude the stock
    rows = walk(dates, 80, phase=2)
    rows[10] = (rows[10][0], 0, 0, 0, 0, 0)
    tp = os.path.join(str(tmp_path), "z")
    os.makedirs(tp)
    w, root, cfg = world(tp, {"OLD": rows}, reg_edit=floor)
    assert x4.build_v4(root, cfg, os.path.join(tp, "out"), now=NOW).manifest["files"]["OLD"]["first"] == dates[100]


def test_yearly_rebase_moves_last_year_into_hist_and_renames_the_file(tmp_path):
    """Same stock, one export whose latest session is in 2025 and one in 2026: histEnd moves a year, the 2025 bars move from the tail into
    the hist file, the hist file name changes with its content."""
    d2 = weekdays("2024-06-03", 460)                     # 2024-06 .. 2026-03
    d1 = [d for d in d2 if d <= "2025-11-28"]

    def run(sub, dates):
        tp = os.path.join(str(tmp_path), sub)
        os.makedirs(tp)
        w = World(tp)
        w.dates, w.latest = dates, dates[-1]
        w.etf_rows = {s: walk(dates, 100 + 20 * i, phase=i * 3) for i, s in enumerate(["aaa", "bbb", "ccc"])}
        w.idx_rows = {"nifty 50": walk(dates, 20000, 0.0003, 0.004, 0), "nifty bank": walk(dates, 45000, 0.0004, 0.006, 5)}
        root = w.build(etf_rows={**w.etf_rows, "reli": walk(d2, 1200, phase=2)[:len(dates)]}, year=int(dates[-1][:4]), last_update=dates[-1],
                       holidays={})
        reg = make_registry(default_start="2024-06-03")
        reg["eqHistoryFloor"] = "2024-01-01"
        reg["instruments"].append(eq("RELI"))
        return x4.build_v4(root, write_registry(tp, reg), os.path.join(tp, "out"), now=NOW)
    a, b = run("y2025", d1), run("y2026", d2)
    fa, fb = a.manifest["files"]["RELI"], b.manifest["files"]["RELI"]
    assert a.manifest["histEnd"] == "2024-12-31" and b.manifest["histEnd"] == "2025-12-31"
    assert fa["hist"]["last"] <= "2024-12-31" < fb["hist"]["last"] <= "2025-12-31" and fa["hist"]["path"] != fb["hist"]["path"]
    assert fb["hist"]["bars"] == fa["hist"]["bars"] + sum(1 for d in d2 if "2025-01-01" <= d <= "2025-12-31")
    assert joined(b, "RELI")[:len(d1)] == joined(a, "RELI")                # the rebase moves bars between files, it never changes them


def test_cli_export_v4_and_actions_propose(tmp_path, capsys):
    from dataops import cli
    dates = World(tmp_path).dates
    w, root, cfg = world(tmp_path, {"DMRG": gap_rows(dates, 120, 0.6)})
    out = os.path.join(str(tmp_path), "cli_out")
    assert cli.main(["export", "--v4", "--eod2-data-root", root, "--config", cfg, "--out", out]) == 0
    text = capsys.readouterr().out
    assert "PUBLISHABLE" in text and "EQ    published 1" in text and os.path.isfile(os.path.join(out, "manifest.json"))
    assert cli.main(["actions", "propose", "--eod2-data-root", root, "--config", cfg]) == 0
    text = capsys.readouterr().out
    assert "DMRG,%s,-39.9" % dates[120] in text and "1 unapproved gap(s) in 1 stock(s)" in text
    assert cli.main(["export", "--v4", "--eod2-data-root", root, "--config", cfg, "--out", out]) == 1          # refuses a non-empty folder
