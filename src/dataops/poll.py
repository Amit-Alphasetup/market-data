"""`py -m dataops poll` (plan D3) — processes control-panel requests on the PC. Meant to run every 10 min 08:00-23:59.

1 acquire the lock (busy -> exit 75; the next poll retries) · 2 `git pull --ff-only` · 3 crash recovery: anything left in inflight/ goes to
history/ with `ERROR: interrupted` (never re-run; Apd resubmits) · 4 for each request in file-name order: id already in processed_ids.json
(last 500) -> history DUPLICATE; else move to inflight/, re-validate SERVER-SIDE (same rules as the panel), run the command in-process
(the lock is re-entrant), write history/<file> = request + {result, message, finishedAt}, remember the id · 5 if any request changed the
registry, run `run` once at the end (unless a `run` request was already in this batch) · 6 commit + push data/control and config.
Nothing in a request is ever executed as shell text.
"""
import contextlib
import io
import json
import os
from datetime import datetime

from . import diagnose as dg
from . import eod2_reader as er
from . import lock as lk
from . import ops
from . import publish as pub
from . import repair as rp
from . import requests_spec as rs
from . import run as runner

PROCESSED_KEEP = 500
MESSAGE_MAX = 4000


def control_dir(ctx):
    return os.path.join(ctx.repo, "data", "control")


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _trim(s):
    s = str(s)
    return s if len(s) <= MESSAGE_MAX else s[:MESSAGE_MAX] + " …(truncated)"


def execute(ctx, layout, req, run_fn=None):
    """Run one validated request. -> (result 'OK'|'ERROR', message, mutated_registry: bool, was_run: bool)."""
    t, a = req["type"], req["args"]
    mut = t in rs.MUTATING_REGISTRY
    try:
        if t == "run":
            rc = (run_fn or runner.run)(ctx, layout, verbose=lambda *x: None)
            return ("OK" if rc == 0 else "ERROR"), "run exited %s" % rc, False, True
        if t == "add":
            return "OK", ops.op_add(ctx, a["symbol"], a.get("kind", "ETF"), a.get("engines", []), a.get("assetClass"), a.get("isin", "")), mut, False
        if t == "remove":
            return "OK", ops.op_remove(ctx, a["symbol"]), mut, False
        if t == "set_engines":
            return "OK", ops.op_set_engines(ctx, a["symbol"], a["engines"]), mut, False
        if t == "backfill":
            return "OK", ops.op_backfill(ctx, a["symbol"], a["from"]), mut, False
        if t == "set_listing":
            return "OK", ops.op_set_listing(ctx, a["symbol"], a["date"], a["evidence"]), mut, False
        if t in ("approve_move", "add_split", "flag_bad_print"):
            msg = (ops.op_approve_move(ctx, a["symbol"], a["date"], a["evidence"]) if t == "approve_move"
                   else ops.op_add_split(ctx, a["symbol"], a["date"], a["ratio"], a["evidence"]) if t == "add_split"
                   else ops.op_flag_bad_print(ctx, a["symbol"], a["date"], a["evidence"]))
            rep = rp.repair(ctx, a["symbol"], downloader=_downloader(layout), strict=False)
            return "OK", "%s · %s" % (msg, rp.main_text(rep).replace("\n", " | ")), True, False
        if t == "diagnose":
            text, _ = dg.run(a["symbol"], a.get("from", "0000-00-00"), a.get("to", "9999-99-99"), ctx.eod2_root, ctx.registry_path, False,
                             os.path.join(ctx.repo, "data", "manifest.json"))
            return "OK", text, False, False
        if t == "repair":
            rep = rp.repair(ctx, a["symbol"], a.get("from"), a.get("to"), downloader=_downloader(layout), strict=True)
            return ("OK" if rep["status"] in ("REPAIRED_EXPORT_OK", "NO_CHANGE_NEEDED") else "ERROR"), rp.main_text(rep), False, False
        if t == "rebuild_missing":
            from . import cli
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._rebuild_missing(ctx)
            return ("OK" if rc == 0 else "ERROR"), buf.getvalue().strip(), False, False
    except ops.OpError as e:
        return "ERROR", str(e), False, False
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return "ERROR", "%s: %s" % (type(e).__name__, e), False, False
    return "ERROR", "unhandled type " + t, False, False


def _downloader(layout):
    if os.environ.get("DATAOPS_NO_REACQUIRE"):
        return None
    return rp.make_eod2_downloader(layout)


def _finish(ctx, name, req, result, message, now, processed):
    rec = dict(req) if isinstance(req, dict) else {"raw": str(req)[:500]}
    rec.update({"result": result, "message": _trim(message), "finishedAt": er.utc_now_iso_ist(now)})
    _write_json(os.path.join(control_dir(ctx), "history", name), rec)
    if isinstance(req, dict) and isinstance(req.get("id"), str) and result != "DUPLICATE":
        processed.append(req["id"])
    return rec


def poll(ctx, layout, now=None, run_fn=None, push=True, verbose=print):
    """Returns the exit code: 0 ok, 1 failure, 75 busy. `verbose` gets one line per request."""
    now = now or datetime.now(runner.sy.IST)
    try:
        with lk.DataOpsLock("poll"):
            try:
                pub.git(ctx.repo, "pull", "--ff-only")                                        # 2
            except pub.PublishFail as e:
                verbose("poll: %s" % e)
                return 1
            cdir = control_dir(ctx)
            req_dir, inflight, hist = (os.path.join(cdir, x) for x in ("requests", "inflight", "history"))
            for d in (req_dir, inflight, hist):
                os.makedirs(d, exist_ok=True)
            proc_path = os.path.join(cdir, "processed_ids.json")
            processed = list(_read_json(proc_path, []))
            n_done = 0
            for name in sorted(os.listdir(inflight)):                                         # 3: crash recovery
                if name.startswith("."):
                    continue
                p = os.path.join(inflight, name)
                req = _read_json(p, {"raw": "unreadable"})
                _finish(ctx, name, req, "ERROR", "ERROR: interrupted (the poller stopped while this request was running); resubmit it if still needed", now, processed)
                os.remove(p)
                n_done += 1
                verbose("poll: %s -> ERROR: interrupted" % name)
            mutated, run_requested = False, False
            for name in sorted(os.listdir(req_dir)):                                          # 4
                if name.startswith(".") or not name.endswith(".json"):
                    continue
                src = os.path.join(req_dir, name)
                try:
                    with open(src, encoding="utf-8") as f:
                        req = json.load(f)
                except (OSError, ValueError) as e:
                    _finish(ctx, name, {"raw": "invalid JSON"}, "ERROR", "invalid JSON: %s" % e, now, processed)
                    os.remove(src)
                    n_done += 1
                    continue
                rid = req.get("id") if isinstance(req, dict) else None
                if isinstance(rid, str) and rid in processed:
                    _finish(ctx, name, req, "DUPLICATE", "request id already processed", now, processed)
                    os.remove(src)
                    n_done += 1
                    verbose("poll: %s -> DUPLICATE" % name)
                    continue
                os.replace(src, os.path.join(inflight, name))
                errs = rs.validate_request(req, name)                                         # server-side re-validation
                if errs:
                    result, message, mut, was_run = "ERROR", "invalid request: " + "; ".join(errs), False, False
                else:
                    result, message, mut, was_run = execute(ctx, layout, req, run_fn)
                mutated |= bool(mut and result == "OK")
                run_requested |= was_run
                _finish(ctx, name, req, result, message, now, processed)
                os.remove(os.path.join(inflight, name))
                n_done += 1
                verbose("poll: %s -> %s: %s" % (name, result, _trim(message).splitlines()[0][:160] if message else ""))
            if mutated and not run_requested:                                                 # 5
                rc = (run_fn or runner.run)(ctx, layout, verbose=lambda *x: None)
                verbose("poll: registry changed -> run exited %s" % rc)
            _write_json(proc_path, processed[-PROCESSED_KEEP:])
            if n_done:                                                                        # 6
                pub.git(ctx.repo, "add", "-A", "data/control", "config")
                if pub.git(ctx.repo, "diff", "--cached", "--quiet", check=False).returncode != 0:
                    pub.git(ctx.repo, "commit", "-q", "-m", "control: %d request(s) processed\n\n%s\n" % (n_done, pub.TRAILER))
                    if push:
                        try:
                            pub.push_or_rebase(ctx.repo, allowed_prefixes=("data/control/requests/",))
                        except pub.PublishFail as e:
                            verbose("poll: push failed: %s" % e)
                            return 1
            return 0
    except lk.LockBusy as e:
        verbose(str(e))
        return lk.EXIT_BUSY
