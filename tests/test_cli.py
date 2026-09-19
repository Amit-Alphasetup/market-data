"""D0.5: `py -m dataops ...` end to end in a child process, exactly as the wrapped Task Scheduler action would run it."""
import os
import subprocess
import sys
import time

from conftest import child_env

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
PY = sys.executable


def run(args, lock_file, **kw):
    return subprocess.run([PY, "-m", "dataops"] + args, cwd=SRC, env=child_env(lock_file), capture_output=True, text=True, **kw)


def test_lock_run_exit_codes_propagate(isolated_lock):
    assert run(["lock-run", "--", PY, "-c", "raise SystemExit(0)"], isolated_lock).returncode == 0
    assert run(["lock-run", "--", PY, "-c", "raise SystemExit(7)"], isolated_lock).returncode == 7
    assert run(["lock-run", "--", "no-such-program-zzz"], isolated_lock).returncode == 127


def test_lock_run_powershell_child_like_the_scheduled_task(isolated_lock):
    r = run(["lock-run", "--", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "Write-Host ('token=' + ($env:DATAOPS_LOCK_TOKEN.Length)); exit 5"], isolated_lock)
    assert r.returncode == 5 and "token=32" in r.stdout


def test_lock_run_is_busy_75_when_another_holder_is_alive(isolated_lock):
    holder = subprocess.Popen([PY, "-m", "dataops", "lock-run", "--", PY, "-c", "import time; time.sleep(6)"], cwd=SRC, env=child_env(isolated_lock))
    try:
        for _ in range(100):
            if os.path.exists(isolated_lock):
                break
            time.sleep(0.1)
        r = run(["lock-run", "--", PY, "-c", "print('SHOULD NOT RUN')"], isolated_lock)
        assert r.returncode == 75 and "SHOULD NOT RUN" not in r.stdout and "lock busy" in r.stderr
    finally:
        holder.kill()
        holder.wait()


def test_nested_lock_run_inside_lock_run_passes(isolated_lock):
    r = run(["lock-run", "--", PY, "-m", "dataops", "lock-run", "--", PY, "-c", "print('INNER RAN')"], isolated_lock)
    assert r.returncode == 0 and "INNER RAN" in r.stdout


def test_unknown_command_and_usage(isolated_lock):
    assert run(["frobnicate"], isolated_lock).returncode == 2
    assert run([], isolated_lock).returncode == 2
    assert "lock-run" in run(["--help"], isolated_lock).stderr
