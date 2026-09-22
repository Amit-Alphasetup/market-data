"""EOD2 layout discovery (plan D1 step 1). Read-only. Result is stored once in config/eod2_layout.json.

Anything ambiguous or missing raises LayoutError — the builder must then STOP and ask (CLAUDE.md stop rule 5), never guess.
"""
import json
import os
import re
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAYOUT_PATH = os.path.join(REPO, "config", "eod2_layout.json")
EOD2_SRC = r"C:\dev\eod2\src"


def _native(path):
    """Record a path the way the running OS writes it.

    Windows keeps the historical backslash form byte-for-byte (the stored layout and its tests predate
    any other platform); on Linux/macOS the path is left alone instead of being mangled into backslashes,
    which is what made a layout generated on a cloud runner unusable.
    """
    return path.replace("/", "\\") if os.sep == "\\" else path


class LayoutError(Exception):
    pass


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def discover_layout(eod2_src=EOD2_SRC, interpreter="python"):
    """Inspect an EOD2 install (src dir) and return the layout dict. Verifies every fact against the files themselves."""
    data_root = os.path.join(eod2_src, "eod2_data")
    daily = os.path.join(data_root, "daily")
    meta_path = os.path.join(data_root, "meta.json")
    init_py = os.path.join(eod2_src, "init.py")
    defs_py = os.path.join(eod2_src, "defs", "defs.py")
    for label, p in (("daily folder", daily), ("meta.json", meta_path), ("init.py", init_py), ("defs/defs.py", defs_py)):
        if not os.path.exists(p):
            raise LayoutError("EOD2 %s not found: %s" % (label, p))
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    keys = {"lastUpdate": "lastUpdate" in meta, "holidays": isinstance(meta.get("holidays"), dict),
            "year": isinstance(meta.get("year"), int), "special_sessions": isinstance(meta.get("special_sessions"), list)}
    missing = [k for k, ok in keys.items() if not ok]
    if missing:
        raise LayoutError("meta.json lacks expected keys: %s" % missing)
    csvs = [n for n in os.listdir(daily) if n.lower().endswith(".csv")]
    if not csvs:
        raise LayoutError("no CSV files in " + daily)
    indices = sorted(n for n in csvs if n.lower().startswith("nifty ") or n.lower() in ("nifty 50.csv",))
    sess = os.path.join(data_root, "special_sessions.txt")
    isin_map = os.path.join(data_root, "isin_symbol_map.json")
    isin_csv = os.path.join(data_root, "isin.csv")
    stores_isin = os.path.isfile(isin_map) or os.path.isfile(isin_csv)
    init_src, defs_src = _read(init_py), _read(defs_py)
    dl = re.findall(r"nse\.(equityBhavcopy|indicesBhavcopy|deliveryBhavcopy)\(defs\.dates\.dt\)", init_src)
    if sorted(set(dl)) != ["deliveryBhavcopy", "equityBhavcopy", "indicesBhavcopy"]:
        raise LayoutError("cannot find the single-date download calls (nse.equityBhavcopy/indicesBhavcopy/deliveryBhavcopy) in init.py")
    for fn in ("updateNseEOD", "updateIndexEOD", "adjustNseStocks", "rollback", "updateNseSymbol"):
        if not re.search(r"^def %s\(" % fn, defs_src, re.M):
            raise LayoutError("defs.%s not found in defs/defs.py" % fn)
    appends_only = bool(re.search(r'symFile\.open\("ab"\)', defs_src))
    commit = None
    try:
        commit = subprocess.run(["git", "-C", os.path.dirname(eod2_src), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "layoutVersion": 1,
        "eod2Src": _native(eod2_src),
        "eod2GitCommit": commit,
        "dataRoot": _native(data_root),
        "dailyFolder": _native(daily),
        "dailyFileNaming": "<lowercase symbol or index name>.csv (spaces kept, e.g. 'nifty 50.csv'); SME symbols use '<symbol>_sme.csv'",
        "csvColumns": ["Date", "Open", "High", "Low", "Close", "Volume", "Series", "TOTAL_TRADES", "QTY_PER_TRADE", "DLV_QTY"],
        "indexFilesInDailyFolder": True,
        "indexFilesSample": indices[:10],
        "sessionCalendarIndex": "NIFTY 50",
        "meta": {
            "path": _native(meta_path),
            "lastSyncKey": "lastUpdate",
            "holidaysKey": "holidays",
            "holidaysDateFormat": "%d-%b-%Y",
            "holidaysYearKey": "year",
            "holidaysCoverage": "current calendar year only (meta.year); earlier years are not stored",
            "specialSessionsKey": "special_sessions",
            "specialSessionsDateFormat": "ISO datetime",
            "specialSessionsFile": _native(sess) if os.path.isfile(sess) else None,
            "pendingDeliveryKey": "DLV_PENDING_DATES",
        },
        "isin": {"eod2StoresIsin": stores_isin, "symbolToIsinFile": _native(isin_map) if os.path.isfile(isin_map) else None,
                 "symbolToIsinKey": "sym2isin", "isinCsv": _native(isin_csv) if os.path.isfile(isin_csv) else None},
        "update": {
            "interpreter": interpreter,
            "interpreterNote": "EOD2 runs under the PATH `python` (has the `nse` package); dataops itself runs under `py`.",
            "workingDirectory": _native(eod2_src),
            "entryScript": "init.py",
            "entryCommand": "%s init.py" % interpreter,
            "syncsDatesSince": "meta.lastUpdate (one date per loop; exits 0 when up to date)",
        },
        "singleDate": {
            "downloadFunctions": {"equity": "NSE.equityBhavcopy(date)", "indices": "NSE.indicesBhavcopy(date)", "delivery": "NSE.deliveryBhavcopy(date)"},
            "downloadFolderNote": "NSE(download_folder, server=True): pass a folder under work/ so raw files never land inside EOD2",
            "updateRoutine": "defs.updateNseEOD(bhavFile, deliveryFile) then defs.updateIndexEOD(indexFile) then defs.adjustNseStocks()",
            "updateAppendsOnly": appends_only,
            "updateNote": ("EOD2's own update routine only APPENDS the day's row to the end of each symbol CSV (updateNseSymbol opens the file 'ab'); "
                           "there is no routine that replaces a historical bar in place. Undo of the newest date = defs.rollback(DAILY_FOLDER) / "
                           "deleteLastLineByDate, then re-sync. Repairing an older date therefore has no EOD2-supported path: D2 `repair` reports "
                           "SOURCE_DIFFERS and stops (any write inside EOD2 is a human decision)."),
        },
    }


def load_layout(path=LAYOUT_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_layout(layout, path=LAYOUT_PATH):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(layout, indent=2, ensure_ascii=False) + "\n")


def main(argv=None):
    lay = discover_layout()
    write_layout(lay)
    print("wrote " + LAYOUT_PATH)
    print(json.dumps({k: lay[k] for k in ("eod2Src", "dailyFolder", "eod2GitCommit")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
