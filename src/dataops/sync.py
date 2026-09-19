"""`dataops sync` (plan D2): make sure EOD2 is up to date, without ever running two EOD2 updates at once.

1. EOD2 local patch check: the text in config/eod2_patch.txt must be present in defs/defs.py, else FAIL "EOD2 local patch missing — re-apply".
2. Skip when EOD2's own meta says the expected latest session is already synced.
3. Refuse (busy) while the legacy job or another EOD2 update is running — the legacy scheduled task is not wrapped in the lock until Apd
   applies the elevated command in PENDING_APD.md 1b, so this process check is the defence in depth.
4. Run EOD2's update entry script (`python init.py` in EOD2's src dir, from config/eod2_layout.json), log to logs\\eod2_YYYYMMDD.log.
"""
import os
import subprocess
from datetime import datetime, timedelta, timezone

from . import eod2_reader as er
from . import lock as lk

IST = timezone(timedelta(hours=5, minutes=30))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATCH_FILE = os.path.join(REPO, "config", "eod2_patch.txt")


class SyncFail(Exception):
    pass


def expected_latest_session(now, holidays, cutoff="19:15"):
    """Latest session whose data should be published at `now` (IST): today if it is a session and past the cutoff, else the previous session."""
    hh, mm = (int(x) for x in cutoff.split(":"))
    now = now.astimezone(IST)

    def is_session(d):
        return d.weekday() < 5 and d.isoformat() not in holidays
    d = now.date()
    if not (is_session(d) and (now.hour, now.minute) >= (hh, mm)):
        d -= timedelta(days=1)
    for _ in range(20):
        if is_session(d):
            break
        d -= timedelta(days=1)
    return d.isoformat()


def check_patch(eod2_src, patch_file=PATCH_FILE):
    if not os.path.isfile(patch_file):
        raise SyncFail("config/eod2_patch.txt is missing")
    need = open(patch_file, encoding="utf-8").read().strip()
    defs = os.path.join(eod2_src, "defs", "defs.py")
    if not os.path.isfile(defs):
        raise SyncFail("EOD2 defs.py not found: " + defs)
    if need not in open(defs, encoding="utf-8").read():
        raise SyncFail("EOD2 local patch missing — re-apply (defs.py must contain: %s)" % need)


def legacy_job_running(markers=("publish_data.ps1", "eod2_export.py", "eod2\\src\\init.py", "eod2/src/init.py"), exclude_pid=None):
    """Names of running processes that are the legacy job or an EOD2 update (looked up by command line)."""
    import psutil
    hits = []
    me = os.getpid() if exclude_pid is None else exclude_pid
    for p in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
        try:
            if p.info["pid"] == me:
                continue
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if any(m.lower() in cmd for m in markers):
                hits.append("%s (pid %s)" % (p.info.get("name"), p.info["pid"]))
            elif "init.py" in cmd and (p.info.get("cwd") or "").lower().endswith("eod2\\src"):
                hits.append("%s init.py (pid %s)" % (p.info.get("name"), p.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return hits


def sync(layout, now=None, cutoff="19:15", runner=None, running_check=None, log_dir=None, patch_file=PATCH_FILE):
    """Returns (status, message): status in SKIPPED | SYNCED. Raises SyncFail (FAIL) or lock.LockBusy (busy)."""
    now = now or datetime.now(IST)
    src = layout["eod2Src"]
    check_patch(src, patch_file)
    source = er.Eod2Source(layout["dataRoot"])
    holidays, _ = source.holidays()
    want = expected_latest_session(now, holidays, cutoff)
    have = source.last_update()
    if have and have >= want:
        return "SKIPPED", "EOD2 already synced through %s (expected %s)" % (have, want)
    busy = (running_check or legacy_job_running)()
    if busy:
        raise lk.LockBusy({"pid": None, "command": "EOD2 update already running: " + "; ".join(busy), "startedAt": None})
    log_dir = log_dir or os.path.join(REPO, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "eod2_%s.log" % now.astimezone(IST).strftime("%Y%m%d"))
    cmd = [layout["update"]["interpreter"], layout["update"]["entryScript"]]
    with open(log_path, "ab") as log:
        log.write(("\n=== %s sync: %s (cwd %s) expected %s, had %s ===\n" % (now.isoformat(timespec="seconds"), " ".join(cmd), layout["update"]["workingDirectory"], want, have)).encode("utf-8"))
        log.flush()
        rc = (runner or _run)(cmd, layout["update"]["workingDirectory"], log)
    if rc != 0:
        raise SyncFail("EOD2 update exited %s (see %s)" % (rc, log_path))
    after = er.Eod2Source(layout["dataRoot"]).last_update()
    return "SYNCED", "EOD2 update finished (lastUpdate %s -> %s); log %s" % (have, after, log_path)


def _run(cmd, cwd, log):
    return subprocess.call(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
