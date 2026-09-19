"""D2: scheduler script generation (nothing is registered by these tests)."""
import os
import subprocess
import sys
import tempfile

from dataops import scheduler as sc


def test_script_contents_and_disabled_by_default():
    s = sc.build_script()
    assert s.count("Register-ScheduledTask") == 2 and s.count("Disable-ScheduledTask") == 2
    assert "-Execute 'C:\\Users\\LENOVO\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe'" in s and "py.exe" not in s
    assert "-Argument '-m dataops run'" in s and "-Argument '-m dataops run --only-if-needed'" in s
    assert "-WorkingDirectory 'C:\\dev\\market-data\\src'" in s
    assert "-At '19:25'" in s and "-At '20:30'" in s and "'Monday','Tuesday','Wednesday','Thursday','Friday'" in s
    assert "-StartWhenAvailable" in s and "-MultipleInstances IgnoreNew" in s and "-RunOnlyIfNetworkAvailable" in s
    assert "Disable-ScheduledTask" not in sc.build_script(enabled=True)
    assert sc.enable_script().splitlines() == ["Enable-ScheduledTask -TaskName 'Market Data Run'", "Enable-ScheduledTask -TaskName 'Market Data Retry'"]


def test_quotes_are_escaped_for_powershell():
    assert sc.q("today's") == "today''s"
    s = sc.build_script(tasks=[{"name": "O'Neil", "at": "19:25", "args": "-m x", "desc": "it's fine"}])
    assert "-TaskName 'O''Neil'" in s and "-Description 'it''s fine'" in s


def test_generated_script_is_valid_powershell_without_running_it():
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(sc.build_script() + sc.enable_script())
        path = f.name
    try:
        cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);"
               "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}else{'PARSE OK'}" % path)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        assert r.returncode == 0 and "PARSE OK" in r.stdout, r.stdout + r.stderr
    finally:
        os.unlink(path)
