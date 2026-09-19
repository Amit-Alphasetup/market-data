"""Shared pytest setup: make `dataops` importable and NEVER let a test touch the real lock file."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
FIXTURES = r"C:\dev\fixtures"


@pytest.fixture(autouse=True)
def isolated_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAOPS_LOCK_FILE", str(tmp_path / "test.lock"))
    monkeypatch.delenv("DATAOPS_LOCK_TOKEN", raising=False)
    return str(tmp_path / "test.lock")


def child_env(lock_file, **extra):
    env = os.environ.copy()
    env["DATAOPS_LOCK_FILE"] = lock_file
    env.pop("DATAOPS_LOCK_TOKEN", None)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env
