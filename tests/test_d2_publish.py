"""D2: atomic publish into a temporary bare repo, retention, refusal, push conflicts, live verification, Telegram summary."""
import copy
import json
import os
import shutil

import pytest

from dataops import export as ex
from dataops import notify as nt
from dataops import publish as pub
from eod2_factory import World
from gitrepo import NOW, clone, init_pair, push_file, sh, small_registry_file


def make_work_snapshot(repo, tmp_path, world=None, **kw):
    """Export a valid snapshot into <repo>/work and return (result, world)."""
    w = world or World(tmp_path)
    root = w.build(**kw)
    cfg = small_registry_file(repo)
    res = ex.build_snapshot(root, cfg, os.path.join(repo, "work"), now=NOW, existing_roots=[os.path.join(repo, "work"), os.path.join(repo, "data", "snapshots")])
    return res, w


@pytest.fixture
def pair(tmp_path):
    repo, bare = init_pair(tmp_path)
    return repo, bare


def remote_head(bare):
    return sh("git", "-C", bare, "rev-parse", "main")


def local_head(repo):
    return sh("git", "-C", repo, "rev-parse", "HEAD")


def test_publish_copies_snapshot_pointer_status_and_pushes(tmp_path, pair):
    repo, bare = pair
    res, w = make_work_snapshot(repo, tmp_path)
    st = pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    sid = res.dataset_id
    dest = os.path.join(repo, "data", "snapshots", sid)
    assert st["result"] == "PUBLISHED" and st["datasetId"] == sid and st["latestCompletedSession"] == w.latest and st["summary"] == res.summary
    assert ex.verify_snapshot(dest, res.manifest) == []
    assert open(os.path.join(repo, "data", "manifest.json"), "rb").read() == open(os.path.join(dest, "manifest.json"), "rb").read()   # pointer = byte copy
    on_disk = pub.read_status(repo)
    assert on_disk["result"] == "PUBLISHED" and on_disk["lastRun"] == "2026-03-10T19:41:07+05:30"
    assert remote_head(bare) == local_head(repo)
    msg = sh("git", "-C", repo, "log", "-1", "--format=%B")
    assert msg.startswith("data: %s PASS" % sid) and "Co-Authored-By: Claude <noreply@anthropic.com>" in msg
    assert sh("git", "-C", repo, "log", "-1", "--format=%an") == "Amit-Alphasetup"
    tracked = sh("git", "-C", repo, "ls-files", "data").splitlines()
    assert "data/manifest.json" in tracked and "data/control/status.json" in tracked and "data/snapshots/%s/calendar.json" % sid in tracked
    assert sh("git", "-C", repo, "status", "--short") == ""


def test_publish_refuses_unpublishable_and_touches_nothing(tmp_path, pair):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    mp = os.path.join(repo, "work", res.dataset_id, "manifest.json")
    m = json.load(open(mp))
    m["publishStatus"] = "UNPUBLISHABLE"
    json.dump(m, open(mp, "w"))
    head = local_head(repo)
    with pytest.raises(pub.PublishRefused, match="UNPUBLISHABLE"):
        pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    assert local_head(repo) == head and remote_head(bare) == head
    assert not os.path.exists(os.path.join(repo, "data", "snapshots", res.dataset_id))
    assert pub.read_status(repo)["result"] == "UNPUBLISHABLE"
    with pytest.raises(pub.PublishRefused, match="no snapshot"):
        pub.publish(repo, "1999-01-01-r1", now=NOW, live_verify=False)


def test_publish_never_overwrites_a_published_snapshot(tmp_path, pair):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    good = pub.tree_hash(os.path.join(repo, "data", "snapshots", res.dataset_id))
    with pytest.raises(pub.PublishFail, match="refusing to overwrite"):
        pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    assert pub.tree_hash(os.path.join(repo, "data", "snapshots", res.dataset_id)) == good
    assert pub.read_status(repo)["result"] == "FAIL"


def test_next_export_gets_the_next_revision_and_both_publish(tmp_path, pair):
    repo, bare = pair
    r1, w = make_work_snapshot(repo, tmp_path)
    pub.publish(repo, r1.dataset_id, now=NOW, live_verify=False)
    r2, _ = make_work_snapshot(repo, tmp_path, w)
    assert r2.dataset_id == w.latest + "-r2"
    pub.publish(repo, None, now=NOW, live_verify=False)                                       # None -> newest work snapshot
    assert pub.list_snapshot_ids(os.path.join(repo, "data", "snapshots")) == [r1.dataset_id, r2.dataset_id]
    assert json.load(open(os.path.join(repo, "data", "manifest.json")))["datasetId"] == r2.dataset_id


def test_retention_keeps_newest_ten_and_pinned(tmp_path, pair):
    repo, _ = pair
    snaps = os.path.join(repo, "data", "snapshots")
    ids = ["2026-01-%02d-r1" % d for d in range(1, 15)] + ["2026-01-14-r2"]                  # 15 folders
    for i in ids:
        os.makedirs(os.path.join(snaps, i))
        open(os.path.join(snaps, i, "manifest.json"), "w").write("{}")
    os.makedirs(os.path.join(snaps, "not-a-snapshot"))
    json.dump(["2026-01-02-r1", "1999-01-01-r1"], open(os.path.join(repo, "config", "pinned_snapshots.json"), "w"))
    kept, deleted = pub.retention(snaps, os.path.join(repo, "config", "pinned_snapshots.json"))
    assert kept == ["2026-01-02-r1"] + ids[-10:] and sorted(deleted) == sorted(set(ids[:-10]) - {"2026-01-02-r1"})
    assert os.path.isdir(os.path.join(snaps, "not-a-snapshot")) and pub.id_key("2026-01-14-r10") > pub.id_key("2026-01-14-r9")
    k2, d2 = pub.retention(snaps, os.path.join(repo, "config", "missing.json"), keep=3)
    assert len(k2) == 3 and k2[-1] == "2026-01-14-r2"


def test_publish_applies_retention_and_records_it(tmp_path, pair):
    repo, _ = pair
    snaps = os.path.join(repo, "data", "snapshots")
    for d in range(1, 13):
        os.makedirs(os.path.join(snaps, "2025-12-%02d-r1" % d))
    sh("git", "-C", repo, "add", "-A", "-f", "data")
    open(os.path.join(snaps, "2025-12-01-r1", "manifest.json"), "w").write("{}")
    sh("git", "-C", repo, "add", "-A", "-f", "data")
    sh("git", "-C", repo, "commit", "-q", "-m", "old")
    sh("git", "-C", repo, "push", "-q")
    res, _ = make_work_snapshot(repo, tmp_path)
    st = pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    ids = pub.list_snapshot_ids(snaps)
    assert len(ids) == 10 and ids[-1] == res.dataset_id and "removed" in st["message"]


def test_push_rejected_then_rebase_when_only_requests_changed_remotely(tmp_path, pair):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    other = clone(tmp_path, bare, "other")
    push_file(other, "data/control/requests/20260310T120000Z-run-abcd1234.json", "{}\n", "panel request")
    st = pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)          # ff-pull runs first, so force the race with a second writer:
    assert st["result"] == "PUBLISHED"
    push_file(other, "data/control/requests/another.json", "{}\n", "another request")
    res2, w = make_work_snapshot(repo, tmp_path)
    # race: the remote moves AFTER our pull but BEFORE our push -> simulate by pushing from `other` inside a patched git() once
    real = pub.git
    state = {"n": 0}

    def racing(repo_, *args, check=True):
        if args[:1] == ("add",) and state["n"] == 0:
            state["n"] = 1
            sh("git", "-C", other, "pull", "-q", "--rebase")
            push_file(other, "data/control/requests/raced.json", "{}\n", "raced request")
        return real(repo_, *args, check=check)
    pub.git = racing
    try:
        st2 = pub.publish(repo, res2.dataset_id, now=NOW, live_verify=False)
    finally:
        pub.git = real
    assert st2["result"] == "PUBLISHED" and remote_head(bare) == local_head(repo)
    sh("git", "-C", repo, "ls-files", "data/control/requests/raced.json")
    assert os.path.exists(os.path.join(repo, "data", "control", "requests", "raced.json"))


def test_push_rejected_with_unexpected_remote_change_fails_and_keeps_the_commit_local(tmp_path, pair):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    other = clone(tmp_path, bare, "other")
    real = pub.git
    state = {"n": 0}

    def racing(repo_, *args, check=True):
        if args[:1] == ("add",) and state["n"] == 0:
            state["n"] = 1
            push_file(other, "config/universe.json", "{\"hacked\": true}\n", "unexpected")
        return real(repo_, *args, check=check)
    pub.git = racing
    try:
        with pytest.raises(pub.PublishFail, match="unexpected files.*commit kept locally"):
            pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    finally:
        pub.git = real
    assert pub.read_status(repo)["result"] == "FAIL"
    assert remote_head(bare) != local_head(repo)
    assert sh("git", "-C", repo, "log", "-1", "--format=%s").startswith("data: %s" % res.dataset_id)      # the commit exists locally


def test_ff_pull_failure_is_a_fail(tmp_path, pair):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    other = clone(tmp_path, bare, "other")
    push_file(other, "README.md", "hi\n")
    open(os.path.join(repo, "diverge.txt"), "w").write("x")
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "commit", "-q", "-m", "local divergence")
    with pytest.raises(pub.PublishFail, match="git pull --ff-only failed"):
        pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    assert not os.path.exists(os.path.join(repo, "data", "snapshots", res.dataset_id))


def test_tampered_copy_is_refused_before_anything_is_committed(tmp_path, pair, monkeypatch):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    monkeypatch.setattr(ex, "verify_snapshot", lambda d, m: ["HASH_MISMATCH AAA"])
    head = local_head(repo)
    with pytest.raises(pub.PublishFail, match="re-verification"):
        pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    assert local_head(repo) == head and not os.path.exists(os.path.join(repo, "data", "snapshots", res.dataset_id))


# ── live verification ──────────────────────────────────────────────────────────────────────────────────────────────────
def local_fetch(repo, base="https://example.test/market-data/", tamper=None, missing=()):
    def fetch(url):
        assert url.startswith(base), url
        rel = url[len(base):]
        if rel in missing:
            raise OSError("404 " + rel)
        data = open(os.path.join(repo, rel), "rb").read()
        return data + b" " if tamper and rel.endswith(tamper) else data
    return fetch


def test_verify_live_checks_raw_bytes_of_every_file(tmp_path, pair):
    repo, _ = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    pub.publish(repo, res.dataset_id, now=NOW, live_verify=False)
    base = "https://example.test/market-data/"
    ok = pub.verify_live(base, None, local_fetch(repo))
    assert ok["ok"] and ok["checked"] == 1 + len(res.files) and ok["datasetId"] == res.dataset_id and ok["failures"] == []
    ok2 = pub.verify_live(base, res.dataset_id, local_fetch(repo))
    assert ok2["ok"]
    bad = pub.verify_live(base, None, local_fetch(repo, tamper="etf/AAA.json"))
    assert not bad["ok"] and len(bad["failures"]) == 1 and bad["failures"][0].startswith("AAA: sha256")
    gone = pub.verify_live(base, None, local_fetch(repo, missing=("data/snapshots/%s/etf/BBB.json" % res.dataset_id,)))
    assert not gone["ok"] and "BBB" in gone["failures"][0]
    nomani = pub.verify_live(base, None, lambda url: (_ for _ in ()).throw(OSError("unreachable")))
    assert not nomani["ok"] and "unreachable" in nomani["failures"][0]


def test_publish_records_live_verify_and_pushes_the_status_commit(tmp_path, pair):
    repo, bare = pair
    res, _ = make_work_snapshot(repo, tmp_path)
    waited = []
    st = pub.publish(repo, res.dataset_id, now=NOW, base_url="https://example.test/market-data/", verify_wait=90, fetch=local_fetch(repo), sleep=waited.append)
    assert waited == [90] and st["liveVerify"] == "OK" and pub.read_status(repo)["liveVerify"] == "OK"
    assert remote_head(bare) == local_head(repo) and sh("git", "-C", repo, "log", "-1", "--format=%s").startswith("data: status")
    res2, w = make_work_snapshot(repo, tmp_path)
    st2 = pub.publish(repo, res2.dataset_id, now=NOW, base_url="https://example.test/market-data/", verify_wait=0, fetch=local_fetch(repo, tamper="calendar.json"), sleep=lambda s: None)
    assert st2["result"] == "PUBLISHED" and st2["liveVerify"].startswith("FAIL: calendar: sha256")


# ── Telegram (Q4: skipped without secrets) ───────────────────────────────────────────────────────────────────────────────
def test_notify_message_formats():
    st = {"result": "PUBLISHED", "latestCompletedSession": "2026-09-18", "datasetId": "2026-09-18-r1", "liveVerify": "OK",
          "summary": {"ok": 43, "warn": 1, "excluded": 1, "quarantined": 1}}
    m = {"quarantined": {"MONQ50": {"code": "ANOMALOUS_MOVE_UNRESOLVED"}}}
    assert nt.format_message(st, m) == ("EOD2 2026-09-18 · PUBLISHED · ok 43 · warn 1 · excl 1 · quar 1\n"
                                        "dataset 2026-09-18-r1 · live verify ✓\nquarantined: MONQ50 (ANOMALOUS_MOVE_UNRESOLVED)")
    assert nt.format_message({**st, "liveVerify": "FAIL: x"}).splitlines()[1].endswith("✗")
    assert nt.format_message({"result": "FAIL", "message": "sync: EOD2 update exited 1"}) == "⚠️ EOD2 FAIL — sync: EOD2 update exited 1"


def test_notify_skips_without_secrets_and_never_raises(tmp_path):
    logs = str(tmp_path / "logs")
    assert nt.notify({"result": "PUBLISHED"}, secrets_path=str(tmp_path / "none.json"), log_dir=logs) == "SKIPPED"
    assert "no secrets" in open(os.path.join(logs, "notify.log"), encoding="utf-8").read()
    sec = tmp_path / "telegram.json"
    sec.write_text(json.dumps({"botToken": "123:SECRET", "chatId": "42"}))
    sent = {}

    class R:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b"{}"

    def opener(req, timeout=0):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return R()
    assert nt.notify({"result": "FAIL", "message": "boom"}, secrets_path=str(sec), opener=opener, log_dir=logs) == "SENT"
    assert sent["url"] == "https://api.telegram.org/bot123:SECRET/sendMessage" and sent["body"] == {"chat_id": "42", "text": "⚠️ EOD2 FAIL — boom"}

    def broken(req, timeout=0):
        raise OSError("network down 123:SECRET")
    assert nt.notify({"result": "PUBLISHED"}, secrets_path=str(sec), opener=broken, log_dir=logs).startswith("ERROR")
    assert "SECRET" not in open(os.path.join(logs, "notify.log"), encoding="utf-8").read()
    sec.write_text("not json")
    assert nt.notify({"result": "PUBLISHED"}, secrets_path=str(sec), opener=opener, log_dir=logs).startswith("ERROR")
