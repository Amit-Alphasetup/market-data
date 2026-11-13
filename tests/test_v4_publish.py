"""v4 publisher (P2 session B): one orphan commit force-pushed to `pub`, refusal on a bad tree, skip when unchanged, revision bump,
live verification over raw bytes - against a local bare repo (tests never touch GitHub)."""
import json
import os
import subprocess

import pytest

from dataops import export_v4 as x4
from dataops import publish_v4 as p4
from eod2_factory import World, walk
from gitrepo import sh
from test_v4_export import NOW, world


@pytest.fixture
def bare(tmp_path):
    b = os.path.join(str(tmp_path), "nse-data.git")
    sh("git", "init", "--bare", "-b", "main", b)
    return b


def show(bare, rel, raw=False):
    r = subprocess.run(["git", "-C", bare, "show", "pub:" + rel], capture_output=True)
    assert r.returncode == 0, r.stderr
    return r.stdout if raw else r.stdout.decode("utf-8")


def make(tmp_path, stocks=None):
    dates = World(tmp_path).dates
    return world(tmp_path, stocks or {"RELI": walk(dates, 1200, phase=2), "M&M": walk(dates, 2800, phase=1)})


def test_publish_is_one_orphan_commit_with_the_tree_under_data_and_exact_bytes(tmp_path, bare):
    w, root, cfg = make(tmp_path)
    r = p4.run_v4(root, cfg, remote=bare, work=os.path.join(str(tmp_path), "w"), now=NOW)
    assert r["result"] == "PUBLISHED" and r["datasetId"] == w.latest + "-r1"
    assert sh("git", "-C", bare, "rev-list", "--count", "pub") == "1"
    m = json.loads(show(bare, "data/manifest.json"))
    assert m["contract"] == "alphadesk-eod2/4" and p4.published_manifest(bare)["datasetId"] == m["datasetId"]
    import hashlib
    for key in ("RELI", "M&M", "NIFTY 50"):
        h = m["files"][key]["hist"]
        assert hashlib.sha256(show(bare, "data/" + h["path"], raw=True)).hexdigest() == h["sha256"], key      # git stored the hashed bytes untouched
    assert hashlib.sha256(show(bare, "data/" + m["tails"]["EQ"]["path"], raw=True)).hexdigest() == m["tails"]["EQ"]["sha256"]
    names = sh("git", "-C", bare, "ls-tree", "--name-only", "pub").split("\n")
    assert ".nojekyll" in names and "index.html" in names and "data" in names
    msg = sh("git", "-C", bare, "log", "-1", "--format=%an|%B", "pub")
    assert msg.startswith("Amit-Alphasetup|data: v4 %s" % m["datasetId"]) and "Co-Authored-By: Claude" in msg


def test_second_run_same_content_is_skipped_and_changed_content_replaces_the_branch(tmp_path, bare):
    w, root, cfg = make(tmp_path)
    work = os.path.join(str(tmp_path), "w")
    first = p4.run_v4(root, cfg, remote=bare, work=work, now=NOW)
    again = p4.run_v4(root, cfg, remote=bare, work=work, now=NOW)
    assert again["result"] == "SKIPPED" and again["datasetId"] == first["datasetId"]
    head = sh("git", "-C", bare, "rev-parse", "pub")
    # the registry changes (a floor) -> same session, different content -> revision 2, still ONE commit on the branch
    reg = json.load(open(cfg, encoding="utf-8"))
    reg["eqHistoryFloor"] = w.dates[50]
    json.dump(reg, open(cfg, "w", encoding="utf-8"))
    third = p4.run_v4(root, cfg, remote=bare, work=work, now=NOW)
    assert third["result"] == "PUBLISHED" and third["datasetId"] == w.latest + "-r2"
    assert sh("git", "-C", bare, "rev-parse", "pub") != head and sh("git", "-C", bare, "rev-list", "--count", "pub") == "1"
    m = json.loads(show(bare, "data/manifest.json"))
    assert m["files"]["RELI"]["first"] == w.dates[50] and m["tails"]["EQ"]["path"] == "tail/EQ.%s-r2.json" % w.latest


def test_tampered_or_unpublishable_tree_is_refused_and_nothing_is_pushed(tmp_path, bare):
    w, root, cfg = make(tmp_path)
    out = os.path.join(str(tmp_path), "tree")
    res = x4.build_v4(root, cfg, out, now=NOW)
    p = os.path.join(out, *res.manifest["files"]["RELI"]["hist"]["path"].split("/"))
    with open(p, "ab") as fh:
        fh.write(b" ")
    with pytest.raises(p4.PublishV4Refused, match="verification"):
        p4.publish_tree(out, remote=bare)
    assert subprocess.run(["git", "-C", bare, "rev-parse", "--verify", "-q", "pub"], capture_output=True).returncode != 0
    out2 = os.path.join(str(tmp_path), "tree2")
    res2 = x4.build_v4(root, cfg, out2, now=NOW)
    m = dict(res2.manifest, publishStatus="UNPUBLISHABLE")
    json.dump(m, open(os.path.join(out2, "manifest.json"), "w", encoding="utf-8"))
    with pytest.raises(p4.PublishV4Refused, match="UNPUBLISHABLE"):
        p4.publish_tree(out2, remote=bare)


def test_a_stale_eod2_copy_never_replaces_a_newer_published_dataset(tmp_path, bare):
    w, root, cfg = make(tmp_path)
    work = os.path.join(str(tmp_path), "w")
    p4.run_v4(root, cfg, remote=bare, work=work, now=NOW)
    head = sh("git", "-C", bare, "rev-parse", "pub")
    old = os.path.join(str(tmp_path), "old")
    os.makedirs(old)
    w2 = World(old)
    w2.dates = w2.dates[:-5]
    w2.etf_rows = {k: v[:-5] for k, v in w2.etf_rows.items()}
    w2.idx_rows = {k: v[:-5] for k, v in w2.idx_rows.items()}
    root2 = w2.build(etf_rows={**w2.etf_rows, "reli": walk(World(tmp_path).dates, 1200, phase=2)[:-5], "m&m": walk(World(tmp_path).dates, 2800, phase=1)[:-5]})
    r = p4.run_v4(root2, cfg, remote=bare, work=work, now=NOW)
    assert r["result"] == "REFUSED" and "NEWER" in r["message"] and sh("git", "-C", bare, "rev-parse", "pub") == head


def test_verify_live_v4_hashes_raw_bytes(tmp_path, bare):
    w, root, cfg = make(tmp_path)
    p4.run_v4(root, cfg, remote=bare, work=os.path.join(str(tmp_path), "w"), now=NOW)
    import urllib.parse
    base = "https://example.test/nse-data/"

    def fetch(url, tamper=None):
        assert url.startswith(base)
        rel = urllib.parse.unquote(url[len(base):])
        data = show(bare, rel, raw=True)
        return data + b" " if tamper and tamper in rel else data
    ok = p4.verify_live_v4(base, symbols=10, fetch=fetch)
    assert ok["ok"] and ok["checked"] == 1 + 3 + 7 and ok["datasetId"] == w.latest + "-r1"       # calendar + 3 tails + 7 history files
    bad = p4.verify_live_v4(base, symbols=10, fetch=lambda u: fetch(u, tamper="hist/EQ/M&M."))
    assert not bad["ok"] and len(bad["failures"]) == 1 and bad["failures"][0].startswith("M&M: sha256")
    gone = p4.verify_live_v4(base, fetch=lambda u: (_ for _ in ()).throw(OSError("404")))
    assert not gone["ok"] and gone["failures"][0].startswith("manifest")
