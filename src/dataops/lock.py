"""D2-LOCK — one cross-process lock shared by the legacy job and every dataops command (plan D0.5 step 1).

* Lock file: <repo>/work/dataops.lock (override with env DATAOPS_LOCK_FILE), JSON {pid, command, startedAt, host, token}.
* Acquire: os.open(O_CREAT|O_EXCL|O_WRONLY). If the file exists: read the holder pid; the holder is ALIVE only if psutil sees the pid
  AND its process name is python(.exe)/py.exe/powershell.exe. Alive -> LockBusy (exit code 75). Dead (or pid recycled by another
  program) -> rename to dataops.lock.dead-<ts>, log, acquire. A lock is NEVER reclaimed because of its age.
* Re-entrancy: the holder exports DATAOPS_LOCK_TOKEN=<token>. A nested dataops call (same process or a child process) that sees a token
  equal to the lock file's token proceeds without re-acquiring; only the outermost holder releases.
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

EXIT_BUSY = 75
TOKEN_ENV = "DATAOPS_LOCK_TOKEN"
ALLOWED_PROCESS_NAMES = ("python.exe", "py.exe", "powershell.exe", "pythonw.exe")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class LockBusy(Exception):
    """Another live dataops process holds the lock (exit code 75)."""
    exit_code = EXIT_BUSY

    def __init__(self, holder):
        self.holder = holder
        super().__init__("dataops lock busy: held by pid %s (%s) since %s" % (holder.get("pid"), holder.get("command"), holder.get("startedAt")))


def lock_path():
    return os.environ.get("DATAOPS_LOCK_FILE") or os.path.join(REPO_ROOT, "work", "dataops.lock")


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_lock(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"pid": None, "command": "<unreadable lock file>", "startedAt": None, "token": None}


def _process_name_ctypes(pid):
    """Windows fallback when psutil is not installed: image name of `pid`, or None if the process does not exist / is not inspectable."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(h)


def process_name(pid, use_psutil=True):
    """Lower-case executable name of a live pid, or None (no such process). psutil if available, else ctypes."""
    if use_psutil:
        try:
            import psutil
        except ImportError:
            psutil = None
        if psutil is not None:
            if not psutil.pid_exists(pid):
                return None
            try:
                return psutil.Process(pid).name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
    return _process_name_ctypes(pid)


def holder_alive(holder, allowed_names=ALLOWED_PROCESS_NAMES, use_psutil=True):
    """True only if pid exists and is one of our own kinds of process (a recycled pid owned by e.g. explorer.exe is NOT alive)."""
    pid = holder.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    name = process_name(pid, use_psutil)
    if name is None:
        return False
    return name in [n.lower() for n in allowed_names] or name.startswith("python")


def _write_exclusive(path, payload):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)


def _log(msg):
    try:
        d = os.path.join(REPO_ROOT, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "lock.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (_now_iso(), msg))
    except OSError:
        pass


class DataOpsLock(contextlib.AbstractContextManager):
    """with DataOpsLock("export"): ...   (re-entrant through DATAOPS_LOCK_TOKEN)"""

    def __init__(self, command="dataops", allowed_names=ALLOWED_PROCESS_NAMES, path=None):
        self.command = command
        self.allowed_names = allowed_names
        self.path = path or lock_path()
        self.token = None
        self.owner = False
        self._prev_env = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        env_token = os.environ.get(TOKEN_ENV)
        if env_token:
            cur = _read_lock(self.path)
            if cur and cur.get("token") == env_token:
                self.token, self.owner = env_token, False   # nested call under the outermost holder
                return self
        payload = {"pid": os.getpid(), "command": self.command, "startedAt": _now_iso(), "host": socket.gethostname(),
                   "token": uuid.uuid4().hex}
        for _ in range(5):
            try:
                _write_exclusive(self.path, payload)
                break
            except FileExistsError:
                holder = _read_lock(self.path)
                if holder is None:            # vanished between open and read: retry
                    continue
                if holder_alive(holder, self.allowed_names):
                    raise LockBusy(holder)
                dead = "%s.dead-%s" % (self.path, time.strftime("%Y%m%d-%H%M%S"))
                try:
                    os.replace(self.path, dead)
                    _log("reclaimed dead lock (pid %s, %s) -> %s" % (holder.get("pid"), holder.get("command"), os.path.basename(dead)))
                except FileNotFoundError:
                    pass                       # another process reclaimed it first; loop and try again
        else:
            raise LockBusy({"pid": None, "command": "<contended>", "startedAt": None})
        self.token, self.owner = payload["token"], True
        self._prev_env = os.environ.get(TOKEN_ENV)
        os.environ[TOKEN_ENV] = self.token
        return self

    def release(self):
        if not self.owner:
            return
        cur = _read_lock(self.path)
        if cur and cur.get("token") == self.token:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
        if self._prev_env is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = self._prev_env
        self.owner = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def lock_run(argv, command=None):
    """py -m dataops lock-run -- <program> <args...>: acquire the lock, run the child with the token in its env, release, return its exit code."""
    if not argv:
        print("lock-run: no program given (usage: lock-run -- <program> <args...>)", file=sys.stderr)
        return 2
    try:
        with DataOpsLock(command or "lock-run: " + " ".join(argv)):
            try:
                return subprocess.call(argv, env=os.environ.copy())
            except FileNotFoundError as e:
                print("lock-run: cannot start %r: %s" % (argv[0], e), file=sys.stderr)
                return 127
    except LockBusy as e:
        print(str(e), file=sys.stderr)
        return EXIT_BUSY
