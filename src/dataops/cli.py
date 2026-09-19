"""py -m dataops <command> ...   Mutating commands run inside the dataops lock (exit 75 when another live dataops/legacy job holds it)."""
import argparse
import json
import os
import sys

from . import lock as lk

USAGE = """usage: py -m dataops <command> [args]
  sync                              make sure EOD2 is up to date (patch check, skip if synced, never concurrent with the legacy job)
  export [--out DIR]                build a v3 snapshot into work/ (read-only on EOD2)
  publish [--id ID] [--no-verify]   atomic publish of a work/ snapshot to the market-data repo (never alphadesk)
  run [--only-if-needed]            sync -> export -> publish -> notify
  add SYMBOL [--kind ETF] [--engines LIFO,DM] [--asset-class C] [--isin I]
  remove SYMBOL                     set enabled:false
  set-engines SYMBOL LIFO,DM
  backfill SYMBOL --from DATE       set historyStart, report EOD2's actual first bar
  set-listing SYMBOL DATE --evidence TEXT
  diagnose SYMBOL [--from D --to D] [--compare-yahoo]
  repair SYMBOL [--from D --to D]   reacquire + compare + re-export (never a second source)
  rebuild-missing                   repair every excluded instrument that can be rebuilt, then export + publish
  approve-move SYMBOL DATE --evidence TEXT
  add-split SYMBOL DATE NUM:DEN --evidence TEXT
  verify [--snapshot ID] [--base-url URL]   fetch every file from the live URL, sha256 over raw bytes
  status                            print data/control/status.json
  reconcile --app-universe FILE [--apply]
  gen-v2 [--dry-run]                regenerate C:\\dev\\eod2_universe.json from config/universe.json
  lock-run -- <program> <args...>   run a program while holding the lock
  poll                              process control-panel requests (data/control/requests)
"""


def _ctx():
    from .ops import Ctx
    c = Ctx()
    c.repo = os.environ.get("DATAOPS_REPO", c.repo)
    c.eod2_root = os.environ.get("DATAOPS_EOD2_ROOT", c.eod2_root)
    c.v2_out = os.environ.get("DATAOPS_V2_OUT", c.v2_out)
    c.v2_backup = os.environ.get("DATAOPS_V2_BACKUP", c.v2_backup)
    return c


def _layout(ctx):
    from . import layout as lay
    return lay.load_layout(os.environ.get("DATAOPS_LAYOUT") or os.path.join(ctx.repo, "config", "eod2_layout.json"))


def _downloader(layout):
    from . import repair as rp
    if os.environ.get("DATAOPS_NO_REACQUIRE"):
        return None
    return rp.make_eod2_downloader(layout)


def _locked(name, fn):
    try:
        with lk.DataOpsLock(name):
            return fn()
    except lk.LockBusy as e:
        print(str(e), file=sys.stderr)
        return lk.EXIT_BUSY


def _opcmd(ctx, fn, *a, after_repair=None):
    from .ops import OpError
    try:
        print(fn(ctx, *a))
    except OpError as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    if after_repair:
        from . import repair as rp
        rep = rp.repair(ctx, after_repair, downloader=_downloader(_layout(ctx)), strict=False)
        print(rp.main_text(rep))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
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
        return _locked("gen-v2", lambda: gen_v2.main(rest))
    if cmd == "export":
        return _export(rest)
    ctx = _ctx()
    if cmd == "status":
        from . import publish as pub
        st = pub.read_status(ctx.repo)
        print(json.dumps(st, indent=2, ensure_ascii=False) if st else "no status.json yet")
        return 0
    if cmd == "verify":
        return _verify(ctx, rest)
    if cmd == "sync":
        return _locked("sync", lambda: _sync(ctx))
    if cmd == "publish":
        return _locked("publish", lambda: _publish(ctx, rest))
    if cmd == "run":
        from . import run as runner
        ap = argparse.ArgumentParser(prog="dataops run")
        ap.add_argument("--only-if-needed", action="store_true")
        a = ap.parse_args(rest)
        return runner.run(ctx, _layout(ctx), only_if_needed=a.only_if_needed)          # takes the lock itself
    if cmd == "repair":
        from . import repair as rp
        ap = argparse.ArgumentParser(prog="dataops repair")
        ap.add_argument("symbol")
        ap.add_argument("--from", dest="start")
        ap.add_argument("--to", dest="end")
        a = ap.parse_args(rest)

        def do():
            rep = rp.repair(ctx, a.symbol, a.start, a.end, downloader=_downloader(_layout(ctx)), strict=True)
            print(rp.main_text(rep))
            return 0 if rep["status"] in ("REPAIRED_EXPORT_OK", "NO_CHANGE_NEEDED") else 1
        return _locked("repair " + a.symbol, do)
    if cmd == "rebuild-missing":
        return _locked("rebuild-missing", lambda: _rebuild_missing(ctx))
    if cmd == "poll":
        from . import poll as poller
        return poller.poll(ctx, _layout(ctx))                                            # takes the lock itself
    return _mutations(ctx, cmd, rest)


def _mutations(ctx, cmd, rest):
    from . import ops
    P = argparse.ArgumentParser
    if cmd == "add":
        ap = P(prog="dataops add")
        ap.add_argument("symbol")
        ap.add_argument("--kind", default="ETF")
        ap.add_argument("--engines", default="")
        ap.add_argument("--asset-class", default=None)
        ap.add_argument("--isin", default="")
        a = ap.parse_args(rest)
        return _locked("add", lambda: _opcmd(ctx, ops.op_add, a.symbol, a.kind, a.engines, a.asset_class, a.isin))
    if cmd == "remove":
        ap = P(prog="dataops remove")
        ap.add_argument("symbol")
        a = ap.parse_args(rest)
        return _locked("remove", lambda: _opcmd(ctx, ops.op_remove, a.symbol))
    if cmd == "set-engines":
        ap = P(prog="dataops set-engines")
        ap.add_argument("symbol")
        ap.add_argument("engines")
        a = ap.parse_args(rest)
        return _locked("set-engines", lambda: _opcmd(ctx, ops.op_set_engines, a.symbol, a.engines))
    if cmd == "backfill":
        ap = P(prog="dataops backfill")
        ap.add_argument("symbol")
        ap.add_argument("--from", dest="start", required=True)
        a = ap.parse_args(rest)
        return _locked("backfill", lambda: _opcmd(ctx, ops.op_backfill, a.symbol, a.start))
    if cmd == "set-listing":
        ap = P(prog="dataops set-listing")
        ap.add_argument("symbol")
        ap.add_argument("date")
        ap.add_argument("--evidence", required=True)
        a = ap.parse_args(rest)
        return _locked("set-listing", lambda: _opcmd(ctx, ops.op_set_listing, a.symbol, a.date, a.evidence))
    if cmd == "approve-move":
        ap = P(prog="dataops approve-move")
        ap.add_argument("symbol")
        ap.add_argument("date")
        ap.add_argument("--evidence", required=True)
        a = ap.parse_args(rest)
        return _locked("approve-move", lambda: _opcmd(ctx, ops.op_approve_move, a.symbol, a.date, a.evidence, after_repair=a.symbol.upper()))
    if cmd == "add-split":
        ap = P(prog="dataops add-split")
        ap.add_argument("symbol")
        ap.add_argument("date")
        ap.add_argument("ratio")
        ap.add_argument("--evidence", required=True)
        a = ap.parse_args(rest)
        return _locked("add-split", lambda: _opcmd(ctx, ops.op_add_split, a.symbol, a.date, a.ratio, a.evidence, after_repair=a.symbol.upper()))
    print("unknown command: " + cmd, file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


def _sync(ctx):
    from . import sync as sy
    try:
        status, msg = sy.sync(_layout(ctx))
    except sy.SyncFail as e:
        print("FAIL: %s" % e, file=sys.stderr)
        return 1
    print("%s: %s" % (status, msg))
    return 0


def _publish(ctx, rest):
    from . import publish as pub
    ap = argparse.ArgumentParser(prog="dataops publish")
    ap.add_argument("--id")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(rest)
    try:
        st = pub.publish(ctx.repo, a.id, live_verify=not a.no_verify)
    except pub.PublishRefused as e:
        print("refused: %s" % e, file=sys.stderr)
        return 3
    except pub.PublishFail as e:
        print("FAIL: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


def _verify(ctx, rest):
    from . import publish as pub
    ap = argparse.ArgumentParser(prog="dataops verify")
    ap.add_argument("--snapshot")
    ap.add_argument("--base-url", default=pub.BASE_URL)
    a = ap.parse_args(rest)
    r = pub.verify_live(a.base_url, a.snapshot)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0 if r["ok"] else 1


def _export(argv):
    from . import export as ex
    from . import report as rp
    ap = argparse.ArgumentParser(prog="dataops export")
    ap.add_argument("--eod2-data-root", default=os.environ.get("DATAOPS_EOD2_ROOT", r"C:\dev\eod2\src\eod2_data"))
    ap.add_argument("--config", default=os.path.join(ex.REPO, "config", "universe.json"))
    ap.add_argument("--out", default=os.path.join(ex.REPO, "work"))
    a = ap.parse_args(argv)
    holder = {}

    def do():
        holder["res"] = ex.build_snapshot(a.eod2_data_root, a.config, a.out)
        return 0
    rc = _locked("export", do)
    if rc:
        return rc
    res = holder["res"]
    print(rp.format_report(res))
    if res.out_dir:
        print("snapshot folder: " + res.out_dir)
    return 0 if res.publish_status == "PUBLISHABLE" else 1


REBUILDABLE = ("NOT_IN_EXPORT", "EXPORT_FAILED", "MISSING_EXPECTED_SESSION")


def _rebuild_missing(ctx):
    from . import export as ex
    from . import publish as pub
    from . import repair as rp
    from .ops import load
    reg = load(ctx)
    enabled = {x["symbol"] for x in reg["instruments"] if x["enabled"]}
    sid = pub.latest_work_snapshot(ctx.work)
    mpath = os.path.join(ctx.work, sid, "manifest.json") if sid else os.path.join(ctx.repo, "data", "manifest.json")
    if not os.path.isfile(mpath):
        print("rebuild-missing: no manifest to read (run `export` first)", file=sys.stderr)
        return 1
    m = json.load(open(mpath, encoding="utf-8"))
    todo = sorted(s for s, x in m.get("excluded", {}).items() if s in enabled and x["code"] in REBUILDABLE)
    layout = _layout(ctx)
    for s in todo:
        rep = rp.repair(ctx, s, downloader=_downloader(layout), strict=False, do_export=False)
        print(rp.main_text(rep))
    res = ex.build_snapshot(ctx.eod2_root, ctx.registry_path, ctx.work)
    print("rebuild-missing: %d instrument(s) attempted; new export %s (%s)" % (len(todo), res.dataset_id, res.publish_status))
    if res.publish_status == "PUBLISHABLE":
        try:
            st = pub.publish(ctx.repo, res.dataset_id)
            print("published %s" % st["datasetId"])
        except (pub.PublishFail, pub.PublishRefused) as e:
            print("publish: %s" % e, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
