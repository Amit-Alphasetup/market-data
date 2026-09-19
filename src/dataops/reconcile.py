"""`dataops reconcile --app-universe FILE [--apply]` (plan D0.5 step 4).

FILE: one symbol per line, optionally `SYM,TAG` (TAG = lifo | momentum | both; anything else is ignored). Export it from AlphaDesk with the
console one-liner  copy(S.universe.map(e=>e.sym+','+e.tag).join('\\n'))  (Settings -> Universe).

Groups printed:
  (a) in the app, not in the registry            (with what EOD2 has: ONE file / NONE / AMBIGUOUS)
  (b) in the registry (enabled), not exported     (with reason)
  (c) quarantined by the legacy exporter          (with events)
  (d) exported OK

--apply : for group (a) symbols where EOD2 has exactly one file -> add to the v3 registry (engines from TAG), regenerate the v2 config
          (gen_v2). Symbols with no EOD2 file are reported NOT_IN_EOD2 and not added. Runs inside the dataops lock.
          The legacy job is NOT started unless --run-legacy is also given (it pushes C:\\dev\\alphadesk and updates EOD2 — see HANDOFF).
"""
import argparse
import json
import os
import sys

from . import gen_v2 as g2
from . import lock as lk
from . import migrate_v2 as mig
from . import schema_universe as su

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY = os.path.join(REPO, "config", "universe.json")
LEGACY_MANIFEST = r"C:\dev\alphadesk_data\data\manifest.json"
EOD2_ROOT = r"C:\dev\eod2\src\eod2_data"
LEGACY_JOB = ["powershell", "-ExecutionPolicy", "Bypass", "-File", r"C:\dev\publish_data.ps1"]
TAG_ENGINES = {"lifo": ["LIFO"], "momentum": ["DM"], "both": ["LIFO", "DM"]}


def read_app_universe(path):
    """-> ordered list of (SYMBOL, tag or None); blank lines / '#' comments skipped, duplicates collapsed (first wins)."""
    out, seen = [], set()
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split(",")]
            sym = parts[0].upper()
            tag = parts[1].lower() if len(parts) > 1 and parts[1].lower() in TAG_ENGINES else None
            if sym not in seen:
                seen.add(sym)
                out.append((sym, tag))
    return out


def eod2_daily_index(root):
    """{lowercase stem: [file names]} for <root>/daily/*.csv (read-only listing)."""
    d = os.path.join(root, "daily")
    idx = {}
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.lower().endswith(".csv"):
                idx.setdefault(fn[:-4].lower(), []).append(fn)
    return idx


def load_manifest(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    return None


def classify(reg, app, manifest, eod2_idx):
    """Pure grouping. app: [(sym, tag)]. Returns dict with lists a/b/c/d."""
    reg_syms = {x["symbol"]: x for x in reg["instruments"]}
    files = (manifest or {}).get("files", {}) or {}
    quar = (manifest or {}).get("quarantined", {}) or {}
    a = []
    for sym, tag in app:
        if sym in reg_syms:
            continue
        cands = eod2_idx.get(sym.lower(), [])
        state = "NONE" if not cands else ("ONE" if len(cands) == 1 else "AMBIGUOUS")
        valid = bool(su.SYMBOL_RE.match(sym))
        a.append({"symbol": sym, "tag": tag, "eod2": state, "files": cands, "validSymbol": valid})
    b, c, d = [], [], []
    for sym, x in reg_syms.items():
        if not x["enabled"]:
            continue
        if sym in quar:
            ev = quar[sym]
            c.append({"symbol": sym, "events": ev if isinstance(ev, list) else [ev]})
        elif sym in files:
            d.append({"symbol": sym, "first": files[sym].get("first"), "last": files[sym].get("last"), "bars": files[sym].get("bars")})
        else:
            reason = "NOT_IN_EOD2" if sym.lower() not in eod2_idx else ("NOT_IN_LEGACY_MANIFEST" if manifest else "NO_LEGACY_MANIFEST")
            b.append({"symbol": sym, "reason": reason})
    return {"a": a, "b": b, "c": c, "d": d}


def format_report(groups):
    L = []
    L.append("(a) IN APP, NOT IN REGISTRY: %d" % len(groups["a"]))
    for x in groups["a"]:
        L.append("    %-12s tag=%-9s EOD2=%s%s" % (x["symbol"], x["tag"] or "-", x["eod2"], "" if x["validSymbol"] else "  (invalid symbol, skipped)"))
    L.append("(b) IN REGISTRY, NOT EXPORTED: %d" % len(groups["b"]))
    for x in groups["b"]:
        L.append("    %-12s %s" % (x["symbol"], x["reason"]))
    L.append("(c) QUARANTINED BY LEGACY EXPORTER: %d" % len(groups["c"]))
    for x in groups["c"]:
        L.append("    %-12s %s" % (x["symbol"], " | ".join(str(e) for e in x["events"])))
    L.append("(d) EXPORTED OK: %d" % len(groups["d"]))
    L.append("    " + ", ".join(x["symbol"] for x in groups["d"]))
    return "\n".join(L)


def add_to_registry(reg, groups):
    """Add every group-(a) symbol with exactly one EOD2 file. Returns (added, skipped[(sym, reason)])."""
    added, skipped = [], []
    for x in groups["a"]:
        if not x["validSymbol"]:
            skipped.append((x["symbol"], "INVALID_SYMBOL"))
        elif x["eod2"] == "NONE":
            skipped.append((x["symbol"], "NOT_IN_EOD2"))
        elif x["eod2"] == "AMBIGUOUS":
            skipped.append((x["symbol"], "AMBIGUOUS_SYMBOL"))
        else:
            sym = x["symbol"]
            reg["instruments"].append({
                "id": "NSE:" + sym, "symbol": sym, "kind": "ETF", "exchange": "NSE", "isin": "", "name": sym,
                "assetClass": mig.asset_class(sym), "engines": list(TAG_ENGINES.get(x["tag"], [])), "linkedIndex": None,
                "listingDate": None, "listingDateEvidence": None, "delistingDate": None, "aliases": [], "eod2File": None,
                "historyStart": None, "enabled": True})
            added.append(sym)
    su.assert_valid(reg)
    return added, skipped


def save_registry(reg, path=REGISTRY):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(mig.dumps(reg))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dataops reconcile", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app-universe", required=True)
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("--manifest", default=LEGACY_MANIFEST)
    ap.add_argument("--eod2-root", default=EOD2_ROOT)
    ap.add_argument("--v2-out", default=g2.V2_PATH)
    ap.add_argument("--v2-backup", default=g2.V2_BACKUP)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--run-legacy", action="store_true", help="after --apply also run the legacy job under the lock (pushes alphadesk!)")
    a = ap.parse_args(argv)
    with open(a.registry, encoding="utf-8") as f:
        reg = json.load(f)
    su.assert_valid(reg)
    app = read_app_universe(a.app_universe)
    groups = classify(reg, app, load_manifest(a.manifest), eod2_daily_index(a.eod2_root))
    print(format_report(groups))
    if not a.apply:
        return 0
    try:
        with lk.DataOpsLock("reconcile --apply"):
            with open(a.registry, encoding="utf-8") as f:   # re-read inside the lock
                reg = json.load(f)
            groups = classify(reg, app, load_manifest(a.manifest), eod2_daily_index(a.eod2_root))
            added, skipped = add_to_registry(reg, groups)
            print("\nAPPLY: added %d: %s" % (len(added), ", ".join(added) or "-"))
            for sym, why in skipped:
                print("  not added: %-12s %s" % (sym, why))
            if added:
                save_registry(reg, a.registry)
                g2.write_v2(reg, a.v2_out, a.v2_backup)
                print("registry saved; v2 config regenerated: %s" % a.v2_out)
            if a.run_legacy:
                return lk.lock_run(LEGACY_JOB)
            print("legacy job NOT started. To publish now run:  py -m dataops lock-run -- " + " ".join(LEGACY_JOB))
    except lk.LockBusy as e:
        print(str(e), file=sys.stderr)
        return lk.EXIT_BUSY
    return 0


if __name__ == "__main__":
    sys.exit(main())
