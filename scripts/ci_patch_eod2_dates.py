"""D6 phase 3 (cloud only) - fix EOD2's "today" check in a fresh EOD2 checkout's defs/dates.py.

Dates.nextDate() stops the catch-up loop with "All Up To Date. Check again after ..." when
`self.dt.day == curTime.day and curTime.hour < 16` - it compares the day of the MONTH only. A catch-up that
starts a month or more back (every shadow run restarts from the 2026-09-22 mirror seed) therefore stops at the
first past date whose day number equals today's whenever the run starts before 16:00 IST, which the scheduled
runs do (they start ~02:00 IST). Seen 2026-10-22/23: EOD2 stayed at 2026-09-22 / 2026-09-23 and the run
still reported success. The intended check is "is this date today": compare the full date.

Applied only to the disposable checkout in .github/workflows/nightly-shadow.yml; the laptop's C:\\dev\\eod2 is
not touched (Apd 2026-10-24: "Patch the cloud copy"). The data EOD2 downloads for a given date is unchanged.

Usage: python scripts/ci_patch_eod2_dates.py <eod2-src-dir>
Idempotent: exits 0 and prints "already patched" if the fixed line is already present.
"""
import os
import sys

OLD = "if self.dt.day == curTime.day and curTime.hour < 16:"
NEW = "if self.dt.date() == curTime.date() and curTime.hour < 16:"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: ci_patch_eod2_dates.py <eod2-src-dir>", file=sys.stderr)
        return 2
    path = os.path.join(argv[0], "defs", "dates.py")
    src = open(path, encoding="utf-8").read()

    if NEW in src and OLD not in src:
        print("already patched: " + path)
        return 0

    count = src.count(OLD)
    if count != 1:
        print("::error::expected exactly one occurrence of the day-of-month check, found %d - EOD2 source has drifted, do not guess (%s)" % (count, path), file=sys.stderr)
        return 1

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src.replace(OLD, NEW, 1))
    print("patched: " + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
