"""Read-only integrity scan of EOD2's daily CSVs (added 2026-10-18 after the Sep-2026 outage).

EOD2 appends each new session to daily/<symbol>.csv. When a file lacked a trailing newline the next row was glued onto the
previous one (FUSED_ROWS), and reruns re-appended the same session (DUPLICATE_DATE); pandas then crashed on a corporate-action
adjustment and every nightly update failed. This scan finds those shapes plus EMPTY files and RAGGED_ROW (field count differs from
the file's usual count) without writing anything. Fixing stays a manual, approved step (scripts/repair_eod2_csvs.py).
"""
import csv
import glob
import os
import re

DATE_AT = re.compile(r"\d{4}-\d{2}-\d{2},")
REPAIR_HINT = "scripts/repair_eod2_csvs.py <dataRoot> dry  (then apply, after approval — it writes inside EOD2)"


def scan_file(path):
    """Returns None when the file is fine, else {file, problem, line, detail} for the first problem found."""
    name = os.path.basename(path)
    raw = open(path, "rb").read()
    if not raw.strip():
        return {"file": name, "problem": "EMPTY", "line": 0, "detail": "%d bytes" % len(raw)}
    lines = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    seen, widths = {}, {}
    rows = []
    for i, line in enumerate(lines[1:], start=2):            # line 1 is the header
        if not line.strip():
            continue
        if DATE_AT.search(line, 11):                          # a second "YYYY-MM-DD," after the row's own date
            return {"file": name, "problem": "FUSED_ROWS", "line": i, "detail": line[:120]}
        n = line.count(",") + 1 if '"' not in line else len(next(csv.reader([line])))
        rows.append((i, n))
        widths[n] = widths.get(n, 0) + 1
        d = line.split(",", 1)[0]
        if d in seen:
            return {"file": name, "problem": "DUPLICATE_DATE", "line": i, "detail": "%s also on line %d" % (d, seen[d])}
        seen[d] = i
    if len(widths) > 1:
        usual = max(widths, key=widths.get)
        i, n = next((i, n) for i, n in rows if n != usual)
        return {"file": name, "problem": "RAGGED_ROW", "line": i, "detail": "%d fields, usual %d" % (n, usual)}
    return None


def scan_daily(data_root):
    files = sorted(glob.glob(os.path.join(data_root, "daily", "*.csv")))
    bad = [b for b in (scan_file(f) for f in files) if b]
    return {"files": len(files), "bad": bad}


def summary(rep, show=10):
    bad = rep["bad"]
    if not bad:
        return "EOD2 daily CSVs OK (%d files)" % rep["files"]
    head = ", ".join("%s (%s line %d)" % (b["file"], b["problem"], b["line"]) for b in bad[:show])
    more = " +%d more" % (len(bad) - show) if len(bad) > show else ""
    return "%d corrupt EOD2 daily CSV file(s) of %d: %s%s. Repair: %s" % (len(bad), rep["files"], head, more, REPAIR_HINT)
