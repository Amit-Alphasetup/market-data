"""Cloud only - point the installed `nse` package at NSE's new website host.

Since 2026-11-13 NSE answers every https://www.nseindia.com/... request with "301 Moved Permanently" to the same
path on https://www.nseindia.in. The `nse` package EOD2 uses (4.0.1 pinned by eod2/requirements.txt; 5.1.2, the
newest on PyPI, is the same) has the old host hard-coded and treats any non-2xx answer as an error, so its
session warm-up fails and EOD2 exits 1 before it downloads anything:
    Network error connecting to NSE ... ConnectionError('https://www.nseindia.com/option-chain 301: Moved Permanently')
(nightly run 43506066774, session 2026-11-13 not published).

Fix: replace the website host in the package's own files, in this run's Python environment only. The archive
host (https://nsearchives.nseindia.com, where the bhavcopy files come from) still answers 200 and is left alone.
Checked from the laptop on 2026-11-14 with a patched copy of nse 4.0.1: cookies, holidays, corporate actions,
daily-report metadata and the equity / index / delivery bhavcopies for 2026-11-13 all work; the unpatched copy
fails with the 301 above.

Nothing in C:\\dev\\eod2 or the laptop's Python is touched. The files EOD2 downloads are the same files.

Usage: python scripts/ci_patch_nse_domain.py [<nse-package-dir>]   (default: the installed `nse` package)
Idempotent: exits 0 and prints "already patched" when the new host is already in place.
"""
import glob
import importlib.util
import os
import sys

OLD = "https://www.nseindia.com"
NEW = "https://www.nseindia.in"
WARMUP = "/option-chain"          # the session warm-up request lives in transport.py; it must end up on NEW


def _package_dir():
    spec = importlib.util.find_spec("nse")   # locates the package without importing it
    if spec is None or not spec.submodule_search_locations:
        return None
    return list(spec.submodule_search_locations)[0]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("usage: ci_patch_nse_domain.py [<nse-package-dir>]", file=sys.stderr)
        return 2
    pkg = argv[0] if argv else _package_dir()
    if not pkg or not os.path.isdir(pkg):
        print("::error::the nse package was not found (%s)" % pkg, file=sys.stderr)
        return 1

    files = sorted(glob.glob(os.path.join(pkg, "*.py")))
    texts = {f: open(f, encoding="utf-8").read() for f in files}
    total = sum(t.count(OLD) for t in texts.values())
    transport = texts.get(os.path.join(pkg, "transport.py"), "")

    if total == 0:
        if NEW + WARMUP in transport:
            print("already patched: " + pkg)
            return 0
        print("::error::neither %s nor %s%s found in the nse package - it has drifted, do not guess (%s)"
              % (OLD, NEW, WARMUP, pkg), file=sys.stderr)
        return 1

    if OLD + WARMUP not in transport:
        print("::error::the session warm-up request (%s%s) is not in transport.py - the nse package has drifted, "
              "do not guess (%s)" % (OLD, WARMUP, pkg), file=sys.stderr)
        return 1

    for f, t in texts.items():
        n = t.count(OLD)
        if n:
            with open(f, "w", encoding="utf-8", newline="\n") as out:
                out.write(t.replace(OLD, NEW))
            print("patched: %s (%d)" % (f, n))
    print("nse website host: %s -> %s, %d places" % (OLD, NEW, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
