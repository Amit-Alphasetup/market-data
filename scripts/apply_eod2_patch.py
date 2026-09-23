"""D6 phase 3 — apply config/eod2_patch.txt to a fresh EOD2 checkout's defs/defs.py.

sync.check_patch() only verifies the patch text is present; it has never applied it (that was always a
manual one-time edit on the laptop). The cloud pipeline checks out a clean EOD2 every run, so this has to
run before `dataops sync` on every run, not once.

The exact transformation (verified against the real local patch via `git diff` in the local eod2 checkout, commit
5c8c8dd6): updateNseEOD's delivery-quantity line changes from `else int(dq)` to
`else (int(dq) if str(dq).strip().isdigit() else 0)` so a non-numeric DLV_QTY field (blank/garbage instead
of a 0) does not crash the day's update.

Usage: py scripts/apply_eod2_patch.py <eod2-src-dir> [--patch-file config/eod2_patch.txt]
Idempotent: exits 0 and prints "already patched" if the patch text is already present.
"""
import argparse
import os
import sys

OLD = 'dq = t.TtlTradgVol if t.SctySrs in ("BE", "BZ") else int(dq)'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("eod2_src")
    ap.add_argument("--patch-file", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "eod2_patch.txt"))
    a = ap.parse_args(argv)

    patch_text = open(a.patch_file, encoding="utf-8").read().strip()
    defs_path = os.path.join(a.eod2_src, "defs", "defs.py")
    src = open(defs_path, encoding="utf-8").read()

    if patch_text in src:
        print("already patched: " + defs_path)
        return 0

    count = src.count(OLD)
    if count != 1:
        print("::error::expected exactly one occurrence of the pre-patch line, found %d - EOD2 source has drifted, do not guess (%s)" % (count, defs_path), file=sys.stderr)
        return 1

    new = "dq = t.TtlTradgVol if t.SctySrs in (\"BE\", \"BZ\") else " + patch_text
    src = src.replace(OLD, new, 1)
    if patch_text not in src:
        print("::error::patch applied but verification failed - substituted text does not match eod2_patch.txt", file=sys.stderr)
        return 1

    with open(defs_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    print("patched: " + defs_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
