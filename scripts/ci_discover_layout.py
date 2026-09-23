"""D6 phase 3 — generate a layout.json for the FRESH eod2 checkout on the cloud runner.

config/eod2_layout.json is checked in with Windows paths (C:\\dev\\eod2\\src\\...) from the laptop where it
was discovered; those paths do not exist on a GitHub Actions runner. dataops.layout.discover_layout()
already writes OS-appropriate paths since D6 phase 1 (that fix is what this script exercises) - this just
calls it with the runner's actual eod2 checkout path instead of the hardcoded default, and writes the
result somewhere the workflow points DATAOPS_LAYOUT at, instead of overwriting the laptop's own
config/eod2_layout.json.

Usage: py scripts/ci_discover_layout.py <eod2-src-dir> <out-json-path>
"""
import os
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: ci_discover_layout.py <eod2-src-dir> <out-json-path>", file=sys.stderr)
        return 2
    eod2_src, out_path = argv

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "src"))
    from dataops import layout as lay

    result = lay.discover_layout(eod2_src=os.path.abspath(eod2_src))
    lay.write_layout(result, path=out_path)
    print("wrote " + out_path)
    print("eod2Src=%s eod2GitCommit=%s dailyFolder=%s" % (result["eod2Src"], result["eod2GitCommit"], result["dailyFolder"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
