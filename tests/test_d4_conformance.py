"""D4: 18 conformance cases. The Python client and the JS client must each reproduce the hand-written expected results, and therefore each other."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "conformance"))
import py_runner  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = py_runner.CASES
pytestmark = pytest.mark.skipif(not os.path.isdir(CASES), reason="fixtures missing (py C:\\dev\\fixtures\\make_fixtures.py)")

EXPECTED_NAMES = ["01_valid", "02_hash_mismatch", "03_invalid_date", "04_nonfinite", "05_missing_session", "06_not_yet_listed", "07_listing_unverified", "08_quarantined",
                  "09_excluded", "10_calendar_beyond_verified", "11_calendar_hash_tamper", "12_warmup_insufficient", "13_missing_execution_open", "14_mixed_snapshot",
                  "15_cache_tamper", "16_force_verify", "17_close_only_index", "18_migrated_approval"]


def expected(name):
    with open(os.path.join(CASES, name, "case.json"), encoding="utf-8") as f:
        return json.load(f)["expected"]


@pytest.fixture(scope="module")
def node_results():
    r = subprocess.run(["node", os.path.join(ROOT, "tests", "conformance", "node_runner.mjs"), CASES.replace("\\", "/")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_all_eighteen_cases_exist_and_have_steps_and_expectations():
    assert sorted(os.listdir(CASES)) == EXPECTED_NAMES
    for n in EXPECTED_NAMES:
        with open(os.path.join(CASES, n, "case.json"), encoding="utf-8") as f:
            c = json.load(f)
        assert c["name"] == n and c["desc"] and len(c["steps"]) == len(c["expected"]) >= 1


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_python_client_matches_expected(name):
    assert py_runner.run_case(os.path.join(CASES, name)) == expected(name)


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_js_client_matches_expected(name, node_results):
    assert node_results[name] == expected(name)


def test_python_and_js_results_are_identical_for_every_case(node_results):
    py = {n: py_runner.run_case(os.path.join(CASES, n)) for n in EXPECTED_NAMES}
    assert py == node_results
    assert json.dumps(py, sort_keys=True) == json.dumps(node_results, sort_keys=True)
