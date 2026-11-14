"""Point-in-time index membership (Backtest Lab v2, P6).

Sources (raw files kept in ref-src/, so every interval is reproducible):
  ref-src/nifty-mcwb/mcwb_<mon><yy>.zip   NSE monthly constituent files (archives.nseindia.com/content/indices/),
                                          one Annexure CSV per index: Nifty 50 and Nifty Next 50 (Jr Nifty before 2012).
  ref-src/today/ind_<index>list.csv       today's official lists (niftyindices.com/IndexConstituent/), with ISINs.
Nifty 100 = Nifty 50 + Nifty Next 50 (index definition). Nifty 200 / 500 have no monthly source: not built.

Resolution is one calendar month: a stock in month M's file is a member from the 1st of M (if it was not in the
previous file) until the day before the 1st of the first month whose file lacks it. Missing months are bridged; a change
across a missing month is dated at the first month after the gap, and its evidence says so.

Usage: python scripts/build_membership.py --isin-map <eod2-data>/isin_symbol_map.json [--out config/membership.json]
Exit 1 if any check fails for every month of an index (the index is then left out).
"""
import argparse, csv, io, json, os, re, sys, zipfile, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
SIZE = {"NIFTY 50": 50, "NIFTY NEXT 50": 50, "NIFTY 100": 100}
TODAY_FILE = {"NIFTY 50": "ind_nifty50list.csv", "NIFTY NEXT 50": "ind_niftynext50list.csv", "NIFTY 100": "ind_nifty100list.csv"}
SRC_URL = "https://archives.nseindia.com/content/indices/"


def month_files(src_dir):
    out = []
    for n in os.listdir(src_dir):
        m = re.fullmatch(r"mcwb_([a-z]{3})(\d\d)\.zip", n)
        if m and m.group(1) in MONTHS:
            out.append(("%04d-%02d" % (2000 + int(m.group(2)), MONTHS.index(m.group(1)) + 1), n))
    return sorted(out)


# Lines that legitimately sit in an index on top of its size (measured in P6 from the files themselves):
# symbol -> (from month, to month, reason); outside these months the symbol counts as a normal member.
EXTRA_LINES = {"TATAMTRDVR": ("2016-04", "2017-08", "second share class (DVR) of TATAMOTORS: Nifty 50 held two lines of one company"),
               "ITCHOTELS": ("2025-01", "2025-02", "demerged from ITC in Jan 2025 and held in Nifty 50 until NSE excluded it"),
               "JIOFIN": ("2023-08", "2023-08", "demerged from RELIANCE in Jul 2023 and held in Nifty 50 until NSE excluded it")}


def is_extra(sym, ym):
    e = EXTRA_LINES.get(sym)
    return bool(e) and e[0] <= ym <= e[1]
# NSE's temporary demerger placeholders (DUMMY<name>): not tradeable, carry no prices -> dropped, listed in the output.
PLACEHOLDER = re.compile(r"^DUMMY")


def classify(entry_name):
    n = entry_name.lower()
    if "jr" in n or "next" in n:
        return "NIFTY NEXT 50"
    if n.startswith("nifty") and "mcwb" in n:
        return "NIFTY 50"
    return None


def read_symbols(text):
    """Returns (symbols, defect). defect is set when the Sr. No column is not 1..n (the file drops rows)."""
    syms, nums = [], []
    for row in csv.reader(io.StringIO(text)):
        if len(row) > 2 and row[0].strip().isdigit() and row[1].strip():
            syms.append(row[1].strip().upper())
            nums.append(int(row[0].strip()))
    defect = None if sorted(nums) == list(range(1, len(nums) + 1)) else "Sr. No is not 1..%d (starts %s): file drops rows" % (len(nums), nums[:1])
    return syms, defect


def parse_month(path):
    got, defects = {}, {}
    with zipfile.ZipFile(path) as z:
        for e in z.namelist():
            idx = classify(os.path.basename(e))
            if idx:
                got[idx], d = read_symbols(z.read(e).decode("utf-8-sig", "replace"))
                if d:
                    defects[idx] = d
    return got, defects


class IsinResolver:
    def __init__(self, isin_map, today_lists):
        self.by_sym = {}
        for isin, hist in isin_map.get("isin2hist", {}).items():
            for h in hist:
                self.by_sym.setdefault(h["symbol"].upper(), []).append((h.get("from_date") or "0000", h.get("to_date") or "9999", isin))
        self.sym2isin = {k.upper(): v for k, v in isin_map.get("sym2isin", {}).items()}
        self.today = today_lists  # symbol -> isin from official lists
        self.isin_hist = isin_map.get("isin2hist", {})

    def resolve(self, sym, date):
        c = self.by_sym.get(sym, [])
        inside = [i for f, t, i in c if f <= date <= t]
        if len(inside) == 1:
            return inside[0], "isin_symbol_map"
        if c:  # nearest interval in time (map starts 2011; earlier months use the first interval)
            best = min(c, key=lambda x: 0 if x[0] <= date <= x[1] else min(abs(_days(x[0], date)), abs(_days(x[1], date))))
            return best[2], "isin_symbol_map(nearest)"
        if sym in self.sym2isin:
            return self.sym2isin[sym], "sym2isin"
        if sym in self.today:
            return self.today[sym], "official-list"
        return None, "unresolved"

    def latest_symbol(self, isin, fallback):
        h = self.isin_hist.get(isin)
        return max(h, key=lambda x: x.get("to_date") or "9999")["symbol"].upper() if h else fallback


def _days(a, b):
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def first_of(ym):
    return ym + "-01"


def last_of(ym):
    y, m = int(ym[:4]), int(ym[5:])
    nxt = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return (nxt - dt.timedelta(days=1)).isoformat()


def prev_day(d):
    return (dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat()


def build_intervals(index, snaps, gaps):
    """snaps: [(ym, file, {isin: symbol})] sorted. Returns interval list."""
    open_ = {}  # isin -> interval
    out = []
    prev_ym = None
    for ym, fname, mem in snaps:
        gap_note = ""
        if prev_ym:
            missing = [g for g in gaps if prev_ym < g < ym]
            if missing:
                gap_note = " (no file for %s: change dated at %s)" % (",".join(missing), ym)
        for isin in list(open_):
            if isin not in mem:
                iv = open_.pop(isin)
                iv["to"] = prev_day(first_of(ym))
                iv["evidence"]["removed"] = fname + gap_note
                out.append(iv)
        for isin, sym in mem.items():
            if isin not in open_:
                open_[isin] = {"index": index, "isin": isin, "symbol": sym, "from": first_of(ym), "to": None,
                               "evidence": {"source": SRC_URL, "added": fname + gap_note if prev_ym else fname + " (first file: member on or before this month)"}}
            open_[isin]["symbol"] = sym
            open_[isin]["evidence"]["lastSeen"] = fname
        prev_ym = ym
    out.extend(open_.values())
    return sorted(out, key=lambda r: (r["from"], r["symbol"]))


def members_on(intervals, date):
    return {r["isin"] for r in intervals if r["from"] <= date and (r["to"] is None or date <= r["to"])}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "ref-src"))
    ap.add_argument("--isin-map", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "config", "membership.json"))
    ap.add_argument("--expect-start", help="exit 2 unless every built index starts on this date (gate check)")
    a = ap.parse_args(argv)

    today = {}
    today_sets = {}
    for idx, fn in TODAY_FILE.items():
        with open(os.path.join(a.src, "today", fn), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        today_sets[idx] = {r["ISIN Code"].strip() for r in rows}
        for r in rows:
            today[r["Symbol"].strip().upper()] = r["ISIN Code"].strip()
    with open(a.isin_map, encoding="utf-8") as f:
        res = IsinResolver(json.load(f), today)

    files = month_files(os.path.join(a.src, "nifty-mcwb"))
    yms = [ym for ym, _ in files]
    all_yms, y, m = [], int(yms[0][:4]), int(yms[0][5:])
    while "%04d-%02d" % (y, m) <= yms[-1]:
        all_yms.append("%04d-%02d" % (y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    gaps = [g for g in all_yms if g not in yms]

    snaps = {k: [] for k in SIZE}
    idx_gaps = {k: list(gaps) for k in SIZE}
    unresolved, problems, defective, dropped = {}, {k: {} for k in SIZE}, {}, {}
    for ym, fname in files:
        got, defects = parse_month(os.path.join(a.src, "nifty-mcwb", fname))
        by = {}
        for idx in ("NIFTY 50", "NIFTY NEXT 50"):
            mem = {}
            for s in got.get(idx, []):
                if PLACEHOLDER.match(s):
                    dropped.setdefault(s, []).append("%s %s" % (fname, idx))
                    continue
                isin, how = res.resolve(s, last_of(ym))
                if not isin:
                    unresolved.setdefault(s, []).append(ym)
                    isin = "SYM:" + s
                mem[isin] = s
            by[idx] = mem
        by["NIFTY 100"] = {**by["NIFTY NEXT 50"], **by["NIFTY 50"]}
        bad = {"NIFTY 50": "NIFTY 50" in defects, "NIFTY NEXT 50": "NIFTY NEXT 50" in defects}
        bad["NIFTY 100"] = bad["NIFTY 50"] or bad["NIFTY NEXT 50"]
        for idx in SIZE:
            if bad[idx]:  # a file that drops rows is treated as a missing month for this index
                defective["%s %s" % (fname, idx)] = defects.get(idx) or defects.get("NIFTY NEXT 50") or defects.get("NIFTY 50")
                idx_gaps[idx].append(ym)
                continue
            n = sum(1 for s in by[idx].values() if not is_extra(s, ym))
            if n != SIZE[idx]:
                problems[idx][ym] = "%d members in %s (expected %d)" % (n, fname, SIZE[idx])
            snaps[idx].append((ym, fname, by[idx]))

    out = {"schema": "membership/1", "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "resolution": "month", "missingMonths": gaps, "indices": {}, "unresolvedSymbols": unresolved,
           "defectiveFiles": defective, "placeholdersDropped": dropped, "extraLines": EXTRA_LINES,
           "notBuilt": {"NIFTY 200": "no monthly constituent source found (P6 step 1)", "NIFTY 500": "no monthly constituent source found (P6 step 1)"}}
    report, ok_any = [], False
    for idx in SIZE:
        ivs = build_intervals(idx, snaps[idx], sorted(idx_gaps[idx]))
        for r in ivs:
            r["symbol"] = res.latest_symbol(r["isin"], r["symbol"]) if not r["isin"].startswith("SYM:") else r["symbol"]
        bad_months = sorted(problems[idx])
        # start = first month after the last bad month (every later month passes the count check)
        start_ym = yms[0] if not bad_months else next((ym for ym in yms if ym > bad_months[-1]), None)
        # walk-forward check: intervals open at the last file's month == today's official list
        last_day = last_of(yms[-1])
        walked = members_on(ivs, last_day)
        tod = today_sets[idx]
        walk_ok = walked == tod
        no_evidence = [r for r in ivs if not r["evidence"].get("added")]
        # 12 sampled dates: evenly spaced months from start, counted from the intervals (not from the raw files)
        sample = []
        if start_ym:
            pool = [ym for ym in yms if ym >= start_ym]
            sample = [pool[round(i * (len(pool) - 1) / 11)] for i in range(12)] if len(pool) >= 12 else pool
        sample_counts = {ym: len(members_on(ivs, "%s-15" % ym) - {r["isin"] for r in ivs if is_extra(r["symbol"], ym)}) for ym in sample}
        sample_ok = all(c == SIZE[idx] for c in sample_counts.values())
        passed = bool(start_ym) and walk_ok and not no_evidence and sample_ok
        ok_any |= passed
        entry = {"size": SIZE[idx], "startDate": first_of(start_ym) if passed else None, "lastFile": files[-1][1],
                 "checks": {"countMismatchMonths": {k: problems[idx][k] for k in bad_months}, "sampleCounts": sample_counts,
                            "walkForwardEqualsToday": walk_ok, "walkOnlyInFiles": sorted(walked - tod), "onlyInTodayList": sorted(tod - walked),
                            "intervalsWithoutEvidence": len(no_evidence)},
                 "intervals": ivs if passed else []}
        if idx == "NIFTY 100":
            entry["definition"] = "NIFTY 50 + NIFTY NEXT 50"
        out["indices"][idx] = entry
        report.append("%s: %s start=%s intervals=%d badMonths=%d walk=%s sample=%s" % (
            idx, "PASS" if passed else "FAIL", entry["startDate"], len(ivs), len(bad_months), walk_ok, sample_ok))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    print("\n".join(report))
    if unresolved:
        print("unresolved symbols:", ", ".join(sorted(unresolved)))
    if a.expect_start and any(v["startDate"] != a.expect_start for v in out["indices"].values()):
        return 2
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(main())
