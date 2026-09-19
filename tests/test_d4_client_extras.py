"""D4 extras: Python client behaviour (immutability, no-raise, manifest fetched once, directory cache) + the JS extras via node --test."""
import json
import os
import subprocess
import sys

import pytest

from conftest import FIXTURES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "clients"))
import eod2_client as ec  # noqa: E402

CASE = os.path.join(FIXTURES, "cases", "01_valid")
BASE = "https://cases.test/md/"
pytestmark = pytest.mark.skipif(not os.path.isdir(CASE), reason="fixtures missing")


def fetcher(log=None, over=None):
    over = over or {}

    def f(url):
        if log is not None:
            log.append(url)
        rel = url[len(BASE):].split("?")[0]
        if rel in over:
            v = over[rel]
            if isinstance(v, Exception):
                raise v
            return v
        p = os.path.join(CASE, "root", rel)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as fh:
            return fh.read()
    return f


def test_bars_are_immutable_and_memoized():
    snap = ec.EOD2Client(BASE, cache="memory", fetch=fetcher()).open_snapshot()
    r = snap.get_series("A1E")
    assert r["ok"] and isinstance(r["bars"], tuple) and all(isinstance(b, tuple) for b in r["bars"])
    with pytest.raises(TypeError):
        r["bars"][0][4] = 1
    with pytest.raises(AttributeError):
        r["bars"].append(())
    assert snap.get_series("A1E") is r
    assert snap.get_series("NIFTY_50")["meta"]["symbol"] == "NIFTY 50"


def test_data_problems_never_raise():
    good = json.load(open(os.path.join(CASE, "root", "data", "manifest.json"), encoding="utf-8"))
    cases = [("network down", {"data/manifest.json": OSError("down")}, "SOURCE_UNAVAILABLE"), ("404", {}, None),
             ("not json", {"data/manifest.json": b"<html>"}, "SCHEMA_INVALID"), ("nan", {"data/manifest.json": b'{"contract":NaN}'}, "SCHEMA_INVALID"),
             ("bad utf8", {"data/manifest.json": b"\xff\xfe"}, "SCHEMA_INVALID"), ("wrong contract", {"data/manifest.json": b'{"contract":"x"}'}, "SCHEMA_INVALID"),
             ("unpublishable", {"data/manifest.json": json.dumps(dict(good, publishStatus="UNPUBLISHABLE")).encode()}, "SOURCE_UNAVAILABLE")]
    for name, over, code in cases:
        if name == "404":
            r = ec.EOD2Client(BASE, cache="memory", fetch=lambda u: None).open_snapshot()
            code = "SOURCE_UNAVAILABLE"
        else:
            r = ec.EOD2Client(BASE, cache="memory", fetch=fetcher(over=over)).open_snapshot()
        assert isinstance(r, dict) and r["ok"] is False and r["code"] == code, (name, r)
    sid = good["datasetId"]
    snap = ec.EOD2Client(BASE, cache="memory", fetch=fetcher(over={"data/snapshots/%s/etf/A1E.json" % sid: OSError("boom"), "data/snapshots/%s/etf/B1E.json" % sid: b"junk"})).open_snapshot()
    assert [snap.get_series("A1E")["code"], snap.get_series("B1E")["code"]] == ["SOURCE_UNAVAILABLE", "HASH_MISMATCH"]
    assert snap.check_coverage("A1E", from_="bad", to="2025-01-02")["code"] == "SCHEMA_INVALID"
    assert snap.check_coverage("A1E", from_="2025-02-01", to="2025-01-02")["code"] == "SCHEMA_INVALID"


def test_manifest_fetched_once_and_only_snapshot_paths_afterwards():
    log = []
    snap = ec.EOD2Client(BASE, cache="memory", fetch=fetcher(log)).open_snapshot()
    snap.verify_all(force=True)
    snap.verify_all()
    assert len([u for u in log if u.endswith("data/manifest.json")]) == 1
    base = BASE + "data/" + snap.manifest["snapshotBase"]
    assert all(u.startswith(base) for u in log if not u.endswith("data/manifest.json"))


def test_directory_cache_persists_across_clients_and_detects_tampering(tmp_path):
    cache = str(tmp_path / "eod2_v3")
    log = []
    c1 = ec.EOD2Client(BASE, cache=cache, fetch=fetcher(log))
    s1 = c1.open_snapshot()
    assert s1.get_series("A1E")["meta"]["cacheStatus"] == "MISS"
    files = os.listdir(cache)
    assert len(files) == 1 and files[0].startswith("eod2v3__%s__A1E__" % s1.id)
    n = len(log)
    c2 = ec.EOD2Client(BASE, cache=cache, fetch=fetcher(log))                      # a new process would see the same folder
    r = c2.open_snapshot().get_series("A1E")
    assert r["meta"]["cacheStatus"] == "HIT" and r["meta"]["fromCache"] and len(log) - n == 2       # manifest + calendar only
    path = os.path.join(cache, files[0])
    b = bytearray(open(path, "rb").read())
    b[len(b) // 2] ^= 1
    open(path, "wb").write(bytes(b))
    r2 = ec.EOD2Client(BASE, cache=cache, fetch=fetcher(log)).open_snapshot().get_series("A1E")
    assert r2["ok"] and r2["meta"]["cacheStatus"] == "REPAIRED" and not r2["meta"]["fromCache"]
    assert c1.clear_cache(symbol="A1E") == 1 and os.listdir(cache) == []
    assert ec.EOD2Client(BASE).cache_kind == "dir" and ec.EOD2Client(BASE, cache="memory").cache_kind == "memory"


def test_default_fetch_reads_local_files(tmp_path):
    p = tmp_path / "x.json"
    p.write_bytes(b"hello")
    assert ec._default_fetch(str(p)) == b"hello" and ec._default_fetch("file://" + str(p)) == b"hello"


def test_js_extras_pass_under_node():
    r = subprocess.run(["node", "--test", "--test-timeout=60000", os.path.join(ROOT, "tests", "conformance", "client_extras.test.mjs")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "fail 0" in r.stdout
