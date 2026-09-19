"""Task Scheduler registration for the v3 pipeline (plan D2). Prints / runs a PowerShell script.

"Market Data Run"   Mon-Fri 19:25 IST  -> python -m dataops run                     (sync is skipped if the legacy 19:15 job already synced)
"Market Data Retry" Mon-Fri 20:30 IST  -> python -m dataops run --only-if-needed    (only if status.json has no PUBLISHED result for today's session)
Both: start when available after a missed start, one instance at a time, run as the current interactive user (limited).
The interpreter is called by its direct path (never through the `py` launcher, which can start an install/update in a bare environment).
"""
import subprocess
import sys

PY = r"C:\Users\LENOVO\AppData\Local\Python\pythoncore-3.14-64\python.exe"
WORKDIR = r"C:\dev\market-data\src"

TASKS = [
    {"name": "Market Data Run", "at": "19:25", "args": "-m dataops run", "desc": "dataops run: sync -> export -> publish -> notify (v3 market-data pipeline)"},
    {"name": "Market Data Retry", "at": "20:30", "args": "-m dataops run --only-if-needed", "desc": "dataops run --only-if-needed: retry when today's session is not PUBLISHED"},
]


def q(s):
    """Escape for a PowerShell single-quoted string."""
    return str(s).replace("'", "''")


def build_script(enabled=False, python=PY, workdir=WORKDIR, tasks=TASKS):
    lines = ["$ErrorActionPreference = 'Stop'", "$days = 'Monday','Tuesday','Wednesday','Thursday','Friday'"]
    for t in tasks:
        lines += [
            "$a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s'" % (q(python), q(t["args"]), q(workdir)),
            "$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At '%s'" % q(t["at"]),
            "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)",
            "Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s -Description '%s' -Force | Out-Null" % (q(t["name"]), q(t["desc"])),
        ]
        if not enabled:
            lines.append("Disable-ScheduledTask -TaskName '%s' | Out-Null" % q(t["name"]))
        lines.append("Get-ScheduledTask -TaskName '%s' | Select-Object TaskName,State | Format-Table -AutoSize" % q(t["name"]))
    return "\n".join(lines) + "\n"


def enable_script(tasks=TASKS):
    return "\n".join("Enable-ScheduledTask -TaskName '%s'" % q(t["name"]) for t in tasks) + "\n"


def register(enabled=False):
    """Run the registration in a child PowerShell. Returns (returncode, output)."""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="dataops_tasks_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(build_script(enabled))
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], capture_output=True, text=True)
    finally:
        os.unlink(path)
    return r.returncode, (r.stdout + r.stderr).strip()


if __name__ == "__main__":
    if "--print" in sys.argv:
        print(build_script("--enabled" in sys.argv))
    else:
        rc, out = register("--enabled" in sys.argv)
        print(out)
        sys.exit(rc)
