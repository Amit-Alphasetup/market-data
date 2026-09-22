"""`dataops diagnose SYMBOL [--from D --to D] [--compare-yahoo]` — read-only report (plan D0.5 step 5; D2 `diagnose`).

Prints the EOD2 bars for the range, daily returns, the ratio prev_close/close and its nearest round split ratio, the volume ratio,
the approvals already recorded for the symbol (with dates and provenance) and, optionally, a Yahoo close column. The Yahoo column is
DIAGNOSIS ONLY (AD12): it is never written into any snapshot. Nothing is modified.
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

ROUND_RATIOS = (2, 3, 4, 5, 10, 20, 25, 50, 100)
EOD2_ROOT = r"C:\dev\eod2\src\eod2_data"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANOMALY = 0.15


def _num(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def read_bars(eod2_root, name):
    """[(date, o, h, l, c, vol, series)] sorted, from daily/<name lower>.csv (case-insensitive header match)."""
    path = os.path.join(eod2_root, "daily", name.lower() + ".csv")
    if not os.path.isfile(path):
        raise FileNotFoundError("EOD2 file not found: " + path)
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (rd.fieldnames or [])}
        for r in rd:
            d = (r.get(cols.get("date", "")) or "")[:10]
            if len(d) != 10:
                continue
            g = lambda k: _num(r.get(cols[k])) if k in cols else None  # noqa: E731
            rows.append((d, g("open"), g("high"), g("low"), g("close"), g("volume"), (r.get(cols.get("series", ""), "") or "").strip()))
    rows.sort(key=lambda t: t[0])
    return rows


def nearest_round_ratio(ratio):
    """(ratio value, relative error) of the closest entry in ROUND_RATIOS to `ratio` (ratio > 0). Also considers reciprocals (reverse splits)."""
    best = None
    for r in ROUND_RATIOS:
        for cand in (float(r), 1.0 / r):
            err = abs(ratio - cand) / cand
            if best is None or err < best[1]:
                best = (cand, err)
    return best


def analyse(rows, start, end):
    """Rows inside [start,end] plus the bar before `start` for the first return. -> list of dicts."""
    out, prev = [], None
    for d, o, h, l, c, v, s in rows:
        if d < start:
            prev = (c, v)
            continue
        if d > end:
            break
        rec = {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v, "series": s,
               "ret": None, "ratio": None, "round": None, "roundErr": None, "volRatio": None, "flag": False}
        if prev and prev[0] and c:
            rec["ret"] = c / prev[0] - 1
            rec["ratio"] = prev[0] / c
            rec["round"], rec["roundErr"] = nearest_round_ratio(rec["ratio"])
            rec["flag"] = abs(rec["ret"]) >= ANOMALY
        if prev and prev[1] and v:
            rec["volRatio"] = v / prev[1]
        out.append(rec)
        prev = (c, v)
    return out


def approvals_for(reg, symbol):
    res = []
    for kind, key in (("split", "splits"), ("genuine move", "genuineMoves"), ("bad print (dropped)", "badPrints")):
        for e in reg.get(key, []):
            if e["symbol"] == symbol:
                res.append({"kind": kind, "date": e["date"], "provenance": e["provenance"], "evidence": e.get("evidence"),
                            "approvedAt": e.get("approvedAt"), "note": e.get("note"),
                            "ratio": ("%s:%s" % (e["ratioNum"], e["ratioDen"])) if kind == "split" else None})
    return res


def yahoo_closes(symbol, start, end, opener=None):
    """{date: close} from Yahoo chart API (ETFs listed on NSE use the .NS suffix). Diagnosis only."""
    def ts(d):
        return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s.NS?period1=%d&period2=%d&interval=1d" % (symbol, ts(start) - 5 * 86400, ts(end) + 2 * 86400)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with (opener or urllib.request.urlopen)(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    res = j["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(res["timestamp"], closes):
        if c is not None:
            out[datetime.fromtimestamp(t + 19800, timezone.utc).strftime("%Y-%m-%d")] = c
    return out


def report(symbol, recs, approvals, quarantine_note=None, yahoo=None, yahoo_error=None):
    L = ["DIAGNOSIS %s  (read-only; nothing was changed)" % symbol, ""]
    hdr = "%-10s %10s %10s %10s %10s %12s %8s %8s %13s %8s" % ("date", "open", "high", "low", "close", "volume", "ret", "vol x", "prev/close", "~round")
    if yahoo is not None:
        hdr += " %10s %8s" % ("yahoo", "vs eod2")
    L.append(hdr)
    f = lambda v, w, p=4: ("%*.*f" % (w, p, v)) if v is not None else " " * (w - 1) + "-"  # noqa: E731
    for r in recs:
        line = "%-10s %s %s %s %s %12s %8s %8s %13s %8s" % (
            r["date"], f(r["open"], 10), f(r["high"], 10), f(r["low"], 10), f(r["close"], 10),
            ("%d" % r["volume"]) if r["volume"] is not None else "-",
            ("%+.2f%%" % (r["ret"] * 100)) if r["ret"] is not None else "-",
            ("%.2f" % r["volRatio"]) if r["volRatio"] is not None else "-",
            ("%.4f" % r["ratio"]) if r["ratio"] is not None else "-",
            ("%g (%.1f%%)" % (r["round"], r["roundErr"] * 100)) if r["round"] is not None else "-")
        if yahoo is not None:
            y = yahoo.get(r["date"])
            line += " %10s %8s" % (("%.4f" % y) if y is not None else "-", ("%+.2f%%" % ((y / r["close"] - 1) * 100)) if (y and r["close"]) else "-")
        if r["flag"]:
            line += "   <-- |ret| >= %d%%" % (ANOMALY * 100)
        L.append(line)
    L.append("")
    flagged = [r for r in recs if r["flag"]]
    if flagged:
        L.append("ANOMALIES (|close-to-close return| >= 15%):")
        for r in flagged:
            near = "nearest round split ratio %g (off by %.1f%%)" % (r["round"], r["roundErr"] * 100)
            L.append("  %s  ret %+.2f%%  prev/close %.4f  %s  volume x%s" % (
                r["date"], r["ret"] * 100, r["ratio"], near, ("%.2f" % r["volRatio"]) if r["volRatio"] is not None else "n/a"))
        L.append("  A split would show a ratio close to a round number AND a volume jump of about the same factor; verify on NSE corporate actions "
                 "before `dataops add-split` / `approve-move`.")
    else:
        L.append("No |return| >= 15% in the range.")
    L.append("")
    L.append("EXISTING APPROVALS for %s (per date; a new anomaly date is never covered by an older approval):" % symbol)
    if approvals:
        for a in approvals:
            L.append("  %-12s %s  %s  provenance=%s  evidence=%s  approvedAt=%s%s" % (
                a["kind"], a["date"], ("ratio " + a["ratio"]) if a["ratio"] else "", a["provenance"],
                repr(a["evidence"]), a["approvedAt"], ("  note=" + repr(a["note"])) if a["note"] else ""))
    else:
        L.append("  none")
    if quarantine_note:
        L.append("")
        L.append("LEGACY EXPORTER QUARANTINE: " + quarantine_note)
    if yahoo_error:
        L.append("")
        L.append("YAHOO COMPARISON UNAVAILABLE: " + yahoo_error)
    elif yahoo is not None:
        L.append("")
        L.append("Yahoo column is a DIAGNOSIS-ONLY cross-check (AD12); it never enters a snapshot.")
    return "\n".join(L)


def run(symbol, start, end, eod2_root=EOD2_ROOT, registry_path=None, compare_yahoo=False, manifest_path=None, yahoo_fn=None):
    symbol = symbol.upper()
    rows = read_bars(eod2_root, symbol)
    recs = analyse(rows, start, end)
    reg = {}
    registry_path = registry_path or os.path.join(REPO, "config", "universe.json")
    if os.path.isfile(registry_path):
        with open(registry_path, encoding="utf-8") as f:
            reg = json.load(f)
    qn = None
    if manifest_path and os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8-sig") as f:
            q = (json.load(f).get("quarantined") or {}).get(symbol)
        if q:
            qn = " | ".join(q if isinstance(q, list) else [str(q)])
    yahoo, yerr = None, None
    if compare_yahoo:
        try:
            yahoo = (yahoo_fn or yahoo_closes)(symbol, start, end)
        except Exception as e:  # network / format problems must not break the read-only report
            yerr = "%s: %s" % (type(e).__name__, e)
    return report(symbol, recs, approvals_for(reg, symbol), qn, yahoo, yerr), recs


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dataops diagnose", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("--from", dest="start", default="0000-00-00")
    ap.add_argument("--to", dest="end", default="9999-99-99")
    ap.add_argument("--compare-yahoo", action="store_true")
    ap.add_argument("--eod2-root", default=EOD2_ROOT)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--manifest", default=r"C:\dev\alphadesk_data\data\manifest.json")
    ap.add_argument("--out", default=None, help="also write the report to this file")
    a = ap.parse_args(argv)
    try:
        text, _ = run(a.symbol, a.start, a.end, a.eod2_root, a.registry, a.compare_yahoo, a.manifest)
    except FileNotFoundError as e:
        print("diagnose: " + str(e), file=sys.stderr)
        return 1
    print(text)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
