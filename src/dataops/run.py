"""`dataops run` = sync -> export -> publish -> notify (plan D2). One lock for the whole run; nested calls reuse it."""
import json
import os
from datetime import datetime

from . import eod2_reader as er
from . import export as ex
from . import lock as lk
from . import notify as nt
from . import publish as pub
from . import report as rp
from . import sync as sy
from .ops import Ctx


def already_published(ctx, holidays, now, cutoff="19:15"):
    """Retry-task guard: status.json has a PUBLISHED result for the session expected right now."""
    st = pub.read_status(ctx.repo)
    want = sy.expected_latest_session(now, holidays, cutoff)
    return bool(st and st.get("result") == "PUBLISHED" and (st.get("latestCompletedSession") or "") >= want), want


def run(ctx, layout, now=None, only_if_needed=False, sync_fn=None, publish_kwargs=None, notify_kwargs=None, verbose=print):
    """Returns the process exit code: 0 ok, 1 failure, 75 busy."""
    now = now or datetime.now(sy.IST)
    src = er.Eod2Source(layout["dataRoot"])
    holidays, _ = src.holidays()
    if only_if_needed:
        done, want = already_published(ctx, holidays, now)
        if done:
            verbose("run: session %s already PUBLISHED — nothing to do" % want)
            return 0
    def fail(result, msg):
        st = pub._status(now, result, None, None, None, msg)
        pub.write_status(ctx.repo, st)
        nt.notify(st, **(notify_kwargs or {}))
        verbose("run: " + msg)
        return 1
    try:
        with lk.DataOpsLock("run"):
            try:
                status, msg = (sync_fn or sy.sync)(layout, now=now)
                verbose("sync: %s — %s" % (status, msg))
            except sy.SyncFail as e:
                return fail("FAIL", "sync: %s" % e)
            res = ex.build_snapshot(layout["dataRoot"], ctx.registry_path, ctx.work, now=now)
            verbose(rp.format_report(res))
            if res.publish_status != "PUBLISHABLE":
                if res.dataset_id and res.manifest:
                    pub.write_status(ctx.repo, pub._status(now, "UNPUBLISHABLE", res.dataset_id, res.manifest.get("latestCompletedSession"), res.summary, res.message))
                return fail("UNPUBLISHABLE", "export: UNPUBLISHABLE — %s" % res.message)
            try:
                st = pub.publish(ctx.repo, res.dataset_id, now=now, **(publish_kwargs or {}))
            except (pub.PublishFail, pub.PublishRefused) as e:
                st = pub.read_status(ctx.repo) or pub._status(now, "FAIL", res.dataset_id, None, None, str(e))
                nt.notify(st, res.manifest, **(notify_kwargs or {}))
                verbose("publish: %s" % e)
                return 1
            nt.notify(st, res.manifest, **(notify_kwargs or {}))
            verbose("publish: %s" % st["message"])
            return 0
    except lk.LockBusy as e:
        verbose(str(e))
        return lk.EXIT_BUSY
