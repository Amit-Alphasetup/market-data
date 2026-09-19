"""Registry (v3) -> v2 exporter config generator (plan D0.5 step 3).

From now on ONLY this module writes C:\\dev\\eod2_universe.json (the config read by the unchanged v2 exporter eod2_export.py).
The original v2 file was backed up once to C:\\dev\\eod2_universe.v2.backup.json (never overwritten).

Field mapping (exactly what eod2_export.py reads):
  start           <- defaultHistoryStart          jump_threshold <- legacyV2.jump_threshold (default 0.15)
  etfs            <- enabled instruments (registry order)   indices <- enabled indices (registry order)
  splits          <- [[symbol, date, ratioNum/ratioDen]]     trim_before <- {symbol: historyStart} of enabled instruments
  genuine         <- {"SYM|date": note or evidence}          quarantine  <- verbatim (object form)
  known_no_trade / gap_allow_all <- gapPolicy.*              patches <- legacyV2.patches
"""
import argparse
import json
import os
import shutil
import sys

from . import schema_universe as su

V2_PATH = r"C:\dev\eod2_universe.json"
V2_BACKUP = r"C:\dev\eod2_universe.v2.backup.json"
KEY_ORDER = ("start", "jump_threshold", "etfs", "indices", "splits", "trim_before", "genuine", "quarantine",
             "known_no_trade", "gap_allow_all", "patches")


def _ratio(num, den):
    return num // den if num % den == 0 else num / den


def gen_v2(reg):
    """Pure function: registry dict -> v2 config dict (same key order as the original file)."""
    su.assert_valid(reg)
    enabled = [x for x in reg["instruments"] if x["enabled"]]
    enabled_syms = {x["symbol"] for x in enabled}
    legacy = reg.get("legacyV2", {})
    gp = reg.get("gapPolicy", {})
    genuine = {}
    for m in reg["genuineMoves"]:
        if m["symbol"] in enabled_syms:
            genuine["%s|%s" % (m["symbol"], m["date"])] = m.get("note") if m.get("note") is not None else (m.get("evidence") or "")
    out = {
        "start": reg["defaultHistoryStart"],
        "jump_threshold": legacy.get("jump_threshold", 0.15),
        "etfs": [x["symbol"] for x in enabled],
        "indices": [x["name"] for x in reg["indices"] if x["enabled"]],
        "splits": [[s["symbol"], s["date"], _ratio(s["ratioNum"], s["ratioDen"])] for s in reg["splits"] if s["symbol"] in enabled_syms],
        "trim_before": {x["symbol"]: x["historyStart"] for x in enabled if x["historyStart"]},
        "genuine": genuine,
        "quarantine": reg["quarantine"] if isinstance(reg["quarantine"], dict) else {},
        "known_no_trade": gp.get("known_no_trade", {}),
        "gap_allow_all": gp.get("gap_allow_all", {}),
        "patches": legacy.get("patches", {}),
    }
    return {k: out[k] for k in KEY_ORDER}


def dumps(cfg):
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def write_v2(reg, path=V2_PATH, backup=V2_BACKUP):
    """Back up the pre-existing file once (only if the backup does not exist), then write the generated config."""
    cfg = gen_v2(reg)
    if os.path.isfile(path) and backup and not os.path.exists(backup):
        shutil.copyfile(path, backup)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(cfg))
    return cfg


def load_registry(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dataops gen-v2", description="Regenerate C:\\dev\\eod2_universe.json from config/universe.json")
    ap.add_argument("--registry", default=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config", "universe.json"))
    ap.add_argument("--out", default=V2_PATH)
    ap.add_argument("--backup", default=V2_BACKUP)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    reg = load_registry(a.registry)
    cfg = gen_v2(reg)
    if a.dry_run:
        print(dumps(cfg))
        return 0
    write_v2(reg, a.out, a.backup)
    print("wrote %s (%d ETFs, %d indices, %d splits, %d genuine)" % (a.out, len(cfg["etfs"]), len(cfg["indices"]), len(cfg["splits"]), len(cfg["genuine"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
