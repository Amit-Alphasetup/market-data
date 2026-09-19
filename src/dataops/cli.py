"""py -m dataops <command> ... (commands are added package by package: D0.5 lock-run, reconcile, diagnose, gen-v2; D2 the rest)."""
import sys

from . import lock as lk


def _usage():
    print("usage: py -m dataops <command> [args]\n"
          "  lock-run -- <program> <args...>   run a program while holding the dataops lock\n"
          "  reconcile --app-universe FILE [--apply]\n"
          "  diagnose SYMBOL [--from D --to D] [--compare-yahoo]\n"
          "  gen-v2 [--dry-run]                regenerate C:\\dev\\eod2_universe.json from config/universe.json\n"
          "  export [--out DIR]                build a v3 snapshot into work/ (read-only on EOD2)", file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        return _usage()
    cmd, rest = argv[0], argv[1:]
    if cmd == "lock-run":
        if rest and rest[0] == "--":
            rest = rest[1:]
        return lk.lock_run(rest)
    if cmd == "reconcile":
        from . import reconcile
        return reconcile.main(rest)
    if cmd == "diagnose":
        from . import diagnose
        return diagnose.main(rest)
    if cmd == "gen-v2":
        from . import gen_v2
        try:
            with lk.DataOpsLock("gen-v2"):
                return gen_v2.main(rest)
        except lk.LockBusy as e:
            print(str(e), file=sys.stderr)
            return lk.EXIT_BUSY
    if cmd == "export":
        return _export(rest)
    print("unknown command: " + cmd, file=sys.stderr)
    return _usage()


def _export(argv):
    """py -m dataops export [--eod2-data-root DIR] [--config FILE] [--out DIR]   (inside the lock; read-only on EOD2)"""
    import argparse
    import os

    from . import export as ex
    from . import report as rp
    ap = argparse.ArgumentParser(prog="dataops export")
    ap.add_argument("--eod2-data-root", default=r"C:\dev\eod2\src\eod2_data")
    ap.add_argument("--config", default=os.path.join(ex.REPO, "config", "universe.json"))
    ap.add_argument("--out", default=os.path.join(ex.REPO, "work"))
    a = ap.parse_args(argv)
    try:
        with lk.DataOpsLock("export"):
            res = ex.build_snapshot(a.eod2_data_root, a.config, a.out)
    except lk.LockBusy as e:
        print(str(e), file=sys.stderr)
        return lk.EXIT_BUSY
    print(rp.format_report(res))
    if res.out_dir:
        print("snapshot folder: " + res.out_dir)
    return 0 if res.publish_status == "PUBLISHABLE" else 1


if __name__ == "__main__":
    sys.exit(main())
