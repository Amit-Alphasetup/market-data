"""D0.5 lock tests (plan D0.5 step 6): busy=75, dead pid reclaimed, live pid never reclaimed by age, nested token passes."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import child_env
from dataops import lock as lk

PY = sys.executable


def hold_lock_child(lock_file, seconds):
    """Start a separate python process that holds the lock for `seconds` (via lock-run) and wait until the lock file exists."""
    p = subprocess.Popen([PY, "-m", "dataops", "lock-run", "--", PY, "-c", "import time; time.sleep(%s)" % seconds],
                         env=child_env(lock_file), cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep + "src",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if os.path.exists(lock_file):
            return p
        time.sleep(0.1)
    p.kill()
    raise AssertionError("child never acquired the lock")


def test_acquire_release_creates_and_removes_file(isolated_lock):
    with lk.DataOpsLock("t") as l:
        assert os.path.exists(isolated_lock)
        data = json.load(open(isolated_lock, encoding="utf-8"))
        assert data["pid"] == os.getpid() and data["command"] == "t" and data["token"] == l.token and data["host"] and data["startedAt"]
        assert os.environ[lk.TOKEN_ENV] == l.token
    assert not os.path.exists(isolated_lock)
    assert lk.TOKEN_ENV not in os.environ


def test_second_acquire_from_another_process_is_busy_75(isolated_lock):
    p = hold_lock_child(isolated_lock, 6)
    try:
        with pytest.raises(lk.LockBusy) as e:
            lk.DataOpsLock("second").acquire()
        assert e.value.exit_code == 75 == lk.EXIT_BUSY
        assert e.value.holder["pid"] != os.getpid()
        assert lk.lock_run([PY, "-c", "print('must not run')"]) == 75
    finally:
        p.kill()
        p.wait()


def test_dead_pid_is_reclaimed_and_logged(isolated_lock):
    with open(isolated_lock, "w", encoding="utf-8") as f:
        json.dump({"pid": 4000000 + os.getpid() % 1000, "command": "crashed", "startedAt": "2020-01-01T00:00:00+05:30", "host": "x", "token": "deadbeef"}, f)
    with lk.DataOpsLock("after-crash"):
        assert json.load(open(isolated_lock, encoding="utf-8"))["command"] == "after-crash"
    leftovers = [n for n in os.listdir(os.path.dirname(isolated_lock)) if ".dead-" in n]
    assert len(leftovers) == 1


def test_live_pid_is_never_reclaimed_regardless_of_age(isolated_lock):
    sleeper = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        with open(isolated_lock, "w", encoding="utf-8") as f:
            json.dump({"pid": sleeper.pid, "command": "long job", "startedAt": "2001-01-01T00:00:00+05:30", "host": "x", "token": "abc"}, f)
        ancient = time.mktime((2001, 1, 1, 0, 0, 0, 0, 0, -1))
        os.utime(isolated_lock, (ancient, ancient))           # file is ~25 years old
        with pytest.raises(lk.LockBusy):
            lk.DataOpsLock("impatient").acquire()
        assert os.path.exists(isolated_lock), "a live holder's lock must survive"
        assert json.load(open(isolated_lock, encoding="utf-8"))["token"] == "abc"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_recycled_pid_of_a_foreign_process_counts_as_dead(isolated_lock):
    foreign = subprocess.Popen(["cmd", "/c", "ping -n 20 127.0.0.1 >nul"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        assert not lk.holder_alive({"pid": foreign.pid})       # cmd.exe is not python/py/powershell
        with open(isolated_lock, "w", encoding="utf-8") as f:
            json.dump({"pid": foreign.pid, "command": "old", "startedAt": None, "host": "x", "token": "t"}, f)
        with lk.DataOpsLock("new"):
            pass
    finally:
        foreign.kill()
        foreign.wait()


def test_powershell_and_py_names_are_alive_names():
    import psutil
    me = psutil.Process(os.getpid()).name().lower()
    assert me.startswith("python")
    assert lk.holder_alive({"pid": os.getpid()})
    assert not lk.holder_alive({"pid": None}) and not lk.holder_alive({"pid": -5}) and not lk.holder_alive({})
    assert lk.holder_alive({"pid": os.getpid()}, allowed_names=("nothing.exe",))  # python* prefix rule
    assert "powershell.exe" in lk.ALLOWED_PROCESS_NAMES and "py.exe" in lk.ALLOWED_PROCESS_NAMES and "python.exe" in lk.ALLOWED_PROCESS_NAMES


def test_nested_acquire_in_same_process_passes_and_only_outermost_releases(isolated_lock):
    with lk.DataOpsLock("outer") as outer:
        tok = json.load(open(isolated_lock, encoding="utf-8"))["token"]
        with lk.DataOpsLock("inner") as inner:
            assert inner.owner is False and inner.token == outer.token == tok
            assert json.load(open(isolated_lock, encoding="utf-8"))["command"] == "outer"
        assert os.path.exists(isolated_lock), "inner exit must not release"
    assert not os.path.exists(isolated_lock)


def test_nested_call_in_child_process_with_token_proceeds(isolated_lock):
    code = ("from dataops.lock import DataOpsLock\n"
            "with DataOpsLock('child-nested') as l:\n"
            "    print('OWNER' if l.owner else 'NESTED')\n")
    with lk.DataOpsLock("outer"):
        r = subprocess.run([PY, "-c", code], env=dict(os.environ, DATAOPS_LOCK_FILE=isolated_lock, PYTHONPATH=child_env(isolated_lock)["PYTHONPATH"]),
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "NESTED"


def test_lock_run_wraps_child_and_propagates_exit_code(isolated_lock):
    code = ("import os, json; d = json.load(open(os.environ['DATAOPS_LOCK_FILE'])); "
            "assert os.environ['DATAOPS_LOCK_TOKEN'] == d['token']; raise SystemExit(3)")
    assert lk.lock_run([PY, "-c", code]) == 3
    assert not os.path.exists(isolated_lock), "released after the child exits"
    assert lk.lock_run([PY, "-c", "pass"]) == 0


def test_lock_run_missing_program_is_127_and_releases(isolated_lock):
    assert lk.lock_run(["definitely-not-a-program-xyz"]) == 127
    assert not os.path.exists(isolated_lock)
    assert lk.lock_run([]) == 2


def test_stale_env_token_that_does_not_match_the_file_is_not_trusted(isolated_lock, monkeypatch):
    sleeper = subprocess.Popen([PY, "-c", "import time; time.sleep(20)"])
    try:
        with open(isolated_lock, "w", encoding="utf-8") as f:
            json.dump({"pid": sleeper.pid, "command": "other", "startedAt": None, "host": "x", "token": "REAL"}, f)
        monkeypatch.setenv(lk.TOKEN_ENV, "STALE")
        with pytest.raises(lk.LockBusy):
            lk.DataOpsLock("x").acquire()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_many_processes_race_exactly_one_wins(isolated_lock):
    code = ("import sys, time\n"
            "from dataops import lock\n"
            "try:\n"
            "    with lock.DataOpsLock('racer'):\n"
            "        print('WIN'); sys.stdout.flush(); time.sleep(1.5)\n"
            "except lock.LockBusy:\n"
            "    print('BUSY')\n")
    procs = [subprocess.Popen([PY, "-c", code], env=child_env(isolated_lock), stdout=subprocess.PIPE, text=True) for _ in range(6)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert outs.count("WIN") == 1 and outs.count("BUSY") == 5, outs
    assert not os.path.exists(isolated_lock)


def test_release_after_crash_of_owner_frees_lock_for_next_run(isolated_lock):
    p = subprocess.Popen([PY, "-c", "from dataops import lock; import time; l = lock.DataOpsLock('victim'); l.acquire(); time.sleep(60)"],
                         env=child_env(isolated_lock))
    for _ in range(100):
        if os.path.exists(isolated_lock):
            break
        time.sleep(0.1)
    assert os.path.exists(isolated_lock)
    p.kill()
    p.wait()
    with lk.DataOpsLock("survivor"):
        pass


def test_ctypes_fallback_works_without_psutil(isolated_lock):
    me = lk.process_name(os.getpid(), use_psutil=False)
    assert me and me.startswith("python"), me
    assert lk.process_name(4000000, use_psutil=False) is None
    assert lk.holder_alive({"pid": os.getpid()}, use_psutil=False)
    foreign = subprocess.Popen(["cmd", "/c", "ping -n 20 127.0.0.1 >nul"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        assert lk.process_name(foreign.pid, use_psutil=False) == "cmd.exe"
        assert not lk.holder_alive({"pid": foreign.pid}, use_psutil=False)
    finally:
        foreign.kill()
        foreign.wait()


def test_lock_works_when_psutil_cannot_be_imported(isolated_lock):
    lines = ["import sys", "sys.modules['psutil'] = None", "from dataops import lock",
             "with lock.DataOpsLock('nopsutil'):", "    print('OK')"]
    r = subprocess.run([PY, "-c", os.linesep.join(lines)], env=child_env(isolated_lock), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "OK", r.stderr
