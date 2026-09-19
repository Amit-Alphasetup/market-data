"""`dataops repair SYMBOL [--from D --to D]` (plan D2) — NEVER substitutes a second source (AD12).

1 affected dates (findings for the symbol, or --from/--to) · 2 reacquire each date's raw NSE bhavcopy into work\\repair\\<date>\\ (untouched)
· 3 compare the symbol's row with EOD2's stored bar: MATCH | SOURCE_DIFFERS | ABSENT_IN_SOURCE · 4 identity (SERIES EQ/BE, ISIN = registry ISIN)
· 5 corporate-action diagnosis (report only) · 6 if SOURCE_DIFFERS: EOD2 has NO routine that replaces a historical bar (it only appends —
see config/eod2_layout.json singleDate.updateNote), so the repair STOPS with BLOCKED_NO_EOD2_ROUTINE; nothing is hand-edited and nothing is
written inside EOD2 · 7 re-export into a NEW revision; publish only if the symbol is now OK, else write work\\repair\\<SYMBOL>_report.json.
"""
import csv
import json
import os
import subprocess

from . import eod2_reader as er
from . import export as ex
from . import publish as pub
from .ops import Ctx, load

ETF_SERIES = ("EQ", "BE", "BZ")


def parse_bhav_row(csv_path, symbol):
    """Row of `symbol` from an NSE UDiFF equity bhavcopy CSV (columns TckrSymb, SctySrs, OpnPric, HghPric, LwPric, ClsPric, TtlTradgVol, ISIN)."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("TckrSymb") or "").strip().upper() == symbol.upper() and (r.get("SctySrs") or "").strip() in ("EQ", "BE", "BZ", "SM", "ST", "N1", "GS"):
                num = lambda k: float(r[k]) if (r.get(k) or "").strip() not in ("", "-") else None  # noqa: E731
                return {"series": r["SctySrs"].strip(), "open": num("OpnPric"), "high": num("HghPric"), "low": num("LwPric"), "close": num("ClsPric"),
                        "volume": num("TtlTradgVol"), "isin": (r.get("ISIN") or "").strip()}
    return None


def compare(raw, bar):
    """raw: source row or None; bar: EOD2 stored bar dict or None. -> MATCH | SOURCE_DIFFERS | ABSENT_IN_SOURCE"""
    if raw is None:
        return "ABSENT_IN_SOURCE"
    if bar is None:
        return "SOURCE_DIFFERS"
    for k in ("open", "high", "low", "close"):
        a, b = raw.get(k), bar.get(k)
        if a is None or b is None or round(float(a), 4) != round(float(b), 4):
            return "SOURCE_DIFFERS"
    if raw.get("volume") is not None and bar.get("volume") is not None and round(raw["volume"]) != round(bar["volume"]):
        return "SOURCE_DIFFERS"
    return "MATCH"


def corporate_action_text(prev_close, close, prev_vol=None, vol=None):
    if not prev_close or not close:
        return "no previous close to compare"
    from .diagnose import nearest_round_ratio
    ratio = prev_close / close
    near, err = nearest_round_ratio(ratio)
    volx = (vol / prev_vol) if (vol and prev_vol) else None
    txt = "prev close %.4f, close %.4f, ratio %.4f, nearest round ratio %g (off %.1f%%)%s." % (prev_close, close, ratio, near, err * 100, (", volume x%.2f" % volx) if volx else "")
    if abs(close / prev_close - 1) >= 0.15:
        txt += " Possible split %s — verify on NSE corporate actions, then `add-split` or `approve-move`." % ("%g:1" % near if near >= 1 else "1:%g" % (1 / near))
    return txt


def affected_dates(ctx, symbol, from_date=None, to_date=None):
    """Dates from --from/--to (EOD2 bars in range) or from the findings of the newest manifest (excluded / quarantined entry of the symbol)."""
    if from_date or to_date:
        files = er.Eod2Source(ctx.eod2_root).stems().get(symbol.lower(), [])
        if not files:
            return []
        rows, _ = er.Eod2Source(ctx.eod2_root).read_bars(files[0])
        return [r["date"] for r in rows if (not from_date or r["date"] >= from_date) and (not to_date or r["date"] <= to_date)]
    sid = pub.latest_work_snapshot(ctx.work)
    mpath = os.path.join(ctx.work, sid, "manifest.json") if sid else os.path.join(ctx.repo, "data", "manifest.json")
    if not os.path.isfile(mpath):
        return []
    with open(mpath, encoding="utf-8") as f:
        m = json.load(f)
    entry = m.get("excluded", {}).get(symbol) or m.get("quarantined", {}).get(symbol)
    dates = set()
    for r in (entry or {}).get("reasons", []):
        dates.update(r.get("dates") or [])
        if r.get("date"):
            dates.add(r["date"])
        for e in r.get("events") or []:
            if e.get("date"):
                dates.add(e["date"])
    for e in (entry or {}).get("events") or []:
        if e.get("date"):
            dates.add(e["date"])
    return sorted(dates)


def make_eod2_downloader(layout):
    """downloader(date, folder) -> path of the raw equity bhavcopy CSV. Runs EOD2's OWN download function (NSE.equityBhavcopy) in EOD2's
    interpreter with the download folder set to work\\repair\\<date>, so nothing is written inside EOD2. Needs network + the `nse` package."""
    code = ("import sys\nfrom pathlib import Path\nfrom datetime import datetime\nfrom nse import NSE\n"
            "n = NSE(Path(sys.argv[1]), server=True)\np = n.equityBhavcopy(datetime.fromisoformat(sys.argv[2]))\nn.exit()\nprint(p)\n")

    def download(date, folder):
        os.makedirs(folder, exist_ok=True)
        r = subprocess.run([layout["update"]["interpreter"], "-c", code, folder, date], capture_output=True, text=True, cwd=layout["update"]["workingDirectory"], timeout=300)
        if r.returncode != 0:
            raise RuntimeError("EOD2 download failed for %s: %s" % (date, (r.stderr or r.stdout).strip()[-300:]))
        return r.stdout.strip().splitlines()[-1]
    return download


def repair(ctx, symbol, from_date=None, to_date=None, downloader=None, strict=True, do_export=True, do_publish=True, publish_kwargs=None, export_now=None):
    """Returns a report dict; status: REPAIRED_EXPORT_OK | STILL_BLOCKED | BLOCKED_NO_EOD2_ROUTINE | IDENTITY_MISMATCH | NO_CHANGE_NEEDED | ERROR."""
    symbol = symbol.upper()
    reg = load(ctx)
    inst = next((x for x in reg["instruments"] if x["symbol"] == symbol), None)
    if inst is None:
        return {"symbol": symbol, "status": "ERROR", "message": "%s is not in the registry" % symbol}
    report = {"symbol": symbol, "dates": [], "comparisons": {}, "diagnosis": [], "status": None, "steps": []}
    dates = affected_dates(ctx, symbol, from_date, to_date)
    report["dates"] = dates
    src = er.Eod2Source(ctx.eod2_root)
    files = src.stems().get(symbol.lower(), [])
    bars = {}
    if len(files) == 1:
        rows, _ = src.read_bars(files[0])
        bars = {r["date"]: r for r in rows}
    if downloader is not None and dates:
        differs = False
        for d in dates:
            folder = os.path.join(ctx.work, "repair", d)
            try:
                raw_path = downloader(d, folder)                               # step 2 (raw file stays untouched in work\repair\<date>\)
                raw = parse_bhav_row(raw_path, symbol)
            except Exception as e:
                report["comparisons"][d] = "DOWNLOAD_FAILED: %s" % e
                if strict:
                    report["status"], report["message"] = "ERROR", "reacquire failed for %s: %s" % (d, e)
                    return _write_report(ctx, report)
                continue
            bar = bars.get(d)
            if raw and raw["series"] not in ETF_SERIES or (raw and inst.get("isin") and raw["isin"] != inst["isin"]):     # step 4
                report["comparisons"][d] = "IDENTITY_MISMATCH"
                report["status"] = "IDENTITY_MISMATCH"
                report["message"] = "raw row for %s on %s has SERIES %s / ISIN %s (registry ISIN %s)" % (symbol, d, raw["series"], raw["isin"], inst.get("isin") or "-")
                return _write_report(ctx, report)
            cmp_ = compare(raw, bar)                                           # step 3
            report["comparisons"][d] = cmp_
            differs |= cmp_ == "SOURCE_DIFFERS"
            prev = next((bars[p] for p in sorted(bars, reverse=True) if p < d), None)      # step 5 (report only)
            if raw and raw.get("close") and prev:
                report["diagnosis"].append({"date": d, "text": corporate_action_text(prev["close"], raw["close"], prev.get("volume"), raw.get("volume"))})
        if differs:                                                            # step 6
            report["status"] = "BLOCKED_NO_EOD2_ROUTINE"
            report["message"] = ("SOURCE_DIFFERS on %s: EOD2 has no routine that replaces a historical bar (it only appends); nothing was changed. "
                                 "Decide manually (any write inside C:\\dev\\eod2 is a human step)." % ", ".join(d for d, c in report["comparisons"].items() if c == "SOURCE_DIFFERS"))
            return _write_report(ctx, report)
    if not do_export:
        report["status"], report["message"] = "NO_CHANGE_NEEDED", "no source difference found; export skipped"
        return report
    res = ex.build_snapshot(ctx.eod2_root, ctx.registry_path, ctx.work, now=export_now or ctx.now)     # step 7: new revision
    report["steps"].append("exported %s (%s)" % (res.dataset_id, res.publish_status))
    ok = res.manifest is not None and symbol in res.files
    report["exportSummary"] = res.summary
    if ok and res.publish_status == "PUBLISHABLE" and do_publish:
        st = pub.publish(ctx.repo, res.dataset_id, now=ctx.now, **(publish_kwargs or {}))
        report["steps"].append("published %s" % st["datasetId"])
        report["status"], report["message"] = "REPAIRED_EXPORT_OK", "%s is now exported OK in %s" % (symbol, res.dataset_id)
        return report
    if ok:
        report["status"], report["message"] = "REPAIRED_EXPORT_OK", "%s is exported OK in %s (not published)" % (symbol, res.dataset_id)
        return report
    why = res.excluded.get(symbol) or res.quarantined.get(symbol) or {}
    report["status"], report["message"] = "STILL_BLOCKED", "%s is still not exportable: %s" % (symbol, why.get("code", res.message))
    return _write_report(ctx, report)


def _write_report(ctx, report):
    d = os.path.join(ctx.work, "repair")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "%s_report.json" % report["symbol"]), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main_text(report):
    L = ["REPAIR %s: %s" % (report["symbol"], report["status"]), report.get("message", "")]
    for d, c in report.get("comparisons", {}).items():
        L.append("  %s  %s" % (d, c))
    for x in report.get("diagnosis", []):
        L.append("  %s  %s" % (x["date"], x["text"]))
    for s in report.get("steps", []):
        L.append("  step: " + s)
    return "\n".join(L)

