"""scripts/ci_patch_nse_domain.py - NSE moved its website from www.nseindia.com to www.nseindia.in (301) on
2026-11-13; the nse package's warm-up request must go to the new host or EOD2 exits 1 (run 43506066774)."""
import importlib.util
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ci_patch_nse_domain as patcher  # noqa: E402

# The relevant lines of nse 4.0.1 (transport.py, request_transport.py, NSE.py), as read from the installed package.
TRANSPORT = '''class Transport:
    def _setCookies(self):
        r = self.request("https://www.nseindia.com/option-chain")
        return r.cookies
'''
NSE_PY = '''class NSE:
    base_url = "https://www.nseindia.com/api"
    next_api_url = f"{base_url}/NextApi/apiClient/GetQuoteApi"
    archive_url = "https://nsearchives.nseindia.com"
    headers = {"Referer": "https://www.nseindia.com/get-quotes/equity?symbol=HDFCBANK"}
    lots = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
'''


def make_pkg(tmp_path, transport=TRANSPORT, nse_py=NSE_PY):
    pkg = tmp_path / "nse"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .NSE import NSE\n", encoding="utf-8")
    (pkg / "transport.py").write_text(transport, encoding="utf-8")
    (pkg / "request_transport.py").write_text(transport.replace("Transport", "RequestTransport"), encoding="utf-8")
    (pkg / "NSE.py").write_text(nse_py, encoding="utf-8")
    return pkg


def read(pkg, name):
    return (pkg / name).read_text(encoding="utf-8")


def test_website_host_moves_and_archive_host_stays(tmp_path, capsys):
    pkg = make_pkg(tmp_path)
    assert patcher.main([str(pkg)]) == 0
    assert '"https://www.nseindia.in/option-chain"' in read(pkg, "transport.py")
    assert '"https://www.nseindia.in/option-chain"' in read(pkg, "request_transport.py")
    nse_py = read(pkg, "NSE.py")
    assert 'base_url = "https://www.nseindia.in/api"' in nse_py
    assert '"Referer": "https://www.nseindia.in/get-quotes/equity?symbol=HDFCBANK"' in nse_py
    # the bhavcopy host still answers 200 on the old name - untouched
    assert 'archive_url = "https://nsearchives.nseindia.com"' in nse_py
    assert '"https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"' in nse_py
    for name in ("transport.py", "request_transport.py", "NSE.py", "__init__.py"):
        assert patcher.OLD not in read(pkg, name)
    assert "4 places" in capsys.readouterr().out


def test_idempotent(tmp_path, capsys):
    pkg = make_pkg(tmp_path)
    assert patcher.main([str(pkg)]) == 0
    before = {n: read(pkg, n) for n in ("transport.py", "request_transport.py", "NSE.py")}
    capsys.readouterr()
    assert patcher.main([str(pkg)]) == 0
    assert "already patched" in capsys.readouterr().out
    assert before == {n: read(pkg, n) for n in before}


def test_refuses_a_drifted_or_missing_package(tmp_path, capsys):
    drifted = tmp_path / "a"
    drifted.mkdir()
    pkg = make_pkg(drifted, transport=TRANSPORT.replace("https://www.nseindia.com/option-chain", "https://example.invalid/x"))
    assert patcher.main([str(pkg)]) == 1          # old host elsewhere, but the warm-up request is not where expected
    assert "drifted" in capsys.readouterr().err
    assert 'base_url = "https://www.nseindia.com/api"' in read(pkg, "NSE.py")   # nothing written

    other = tmp_path / "b"
    other.mkdir()
    pkg2 = make_pkg(other, transport="x = 1\n", nse_py="y = 2\n")
    assert patcher.main([str(pkg2)]) == 1          # neither host present
    assert "drifted" in capsys.readouterr().err

    assert patcher.main([str(tmp_path / "nope")]) == 1
    assert "not found" in capsys.readouterr().err


def test_patches_a_copy_of_the_real_installed_package(tmp_path):
    spec = importlib.util.find_spec("nse")     # installed on the laptop and on the nightly runner, not in the tests job
    if spec is None or not spec.submodule_search_locations:
        return
    real = list(spec.submodule_search_locations)[0]
    pkg = tmp_path / "nse"
    shutil.copytree(real, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    if patcher.OLD + patcher.WARMUP not in read(pkg, "transport.py"):
        return                                   # a later nse release that already moved
    assert patcher.main([str(pkg)]) == 0
    assert patcher.NEW + patcher.WARMUP in read(pkg, "transport.py")
    assert 'base_url = "https://www.nseindia.in/api"' in read(pkg, "NSE.py")
    assert 'archive_url = "https://nsearchives.nseindia.com"' in read(pkg, "NSE.py")
    assert patcher.OLD not in read(pkg, "NSE.py")
    # the real installed package itself is not modified by this test
    assert patcher.OLD + patcher.WARMUP in open(os.path.join(real, "transport.py"), encoding="utf-8").read()
