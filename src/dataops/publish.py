"""Atomic publish (plan D2) + retention + status.json + live verification.

publish(): 1 `git pull --ff-only` · 2 copy work/<id> -> data/snapshots/<id> (never overwrite) and re-verify · 3 data/manifest.json = byte copy
· 4 retention (newest 10 + pinned) · 5 data/control/status.json · 6 commit `data: <id> <certification>` and push · 7 push rejected ->
`git pull --rebase` (only data/control/requests/ may have changed remotely; snapshot folder must be unchanged) and push once more, else FAIL
with the commit kept locally · 8 wait, then verify the LIVE URL (raw bytes vs manifest sha256) and record it in status.json.
Only the market-data repo is ever pushed. Runs inside the dataops lock (the CLI takes it).
"""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

from . import export as ex
from . import hashing as H
from . import eod2_reader as er

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_URL = "https://amit-alphasetup.github.io/market-data/"
KEEP_NEWEST = 10
ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-r(\d+)$")
TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


class PublishRefused(Exception):
    """UNPUBLISHABLE (or missing) snapshot: nothing was touched."""


class PublishFail(Exception):
    """Something went wrong after the run started; message is recorded in status.json."""


def id_key(dataset_id):
    m = ID_RE.match(dataset_id)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def list_snapshot_ids(root):
    if not os.path.isdir(root):
        return []
    return sorted((n for n in os.listdir(root) if ID_RE.match(n) and os.path.isdir(os.path.join(root, n))), key=id_key)


def latest_work_snapshot(work):
    for sid in reversed(list_snapshot_ids(work)):
        if os.path.isfile(os.path.join(work, sid, "manifest.json")):
            return sid
    return None


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PublishFail("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def tree_hash(folder):
    """Hash of a folder's file names and bytes (to prove the published snapshot did not change during a rebase)."""
    h = __import__("hashlib").sha256()
    for root, _, files in sorted(os.walk(folder)):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            h.update(os.path.relpath(p, folder).replace("\\", "/").encode())
            h.update(H.sha256_file(p).encode())
    return h.hexdigest()


def retention(snapshots_dir, pinned_file, keep=KEEP_NEWEST):
    """Delete all but the newest `keep` snapshot folders and the pinned ids. Returns (kept, deleted)."""
    pinned = set()
    if os.path.isfile(pinned_file):
        with open(pinned_file, encoding="utf-8") as f:
            data = json.load(f)
        pinned = set(data if isinstance(data, list) else data.get("pinned", []))
    ids = list_snapshot_ids(snapshots_dir)
    keep_set = set(ids[-keep:]) | (pinned & set(ids))
    deleted = []
    for sid in ids:
        if sid not in keep_set:
            shutil.rmtree(os.path.join(snapshots_dir, sid))
            deleted.append(sid)
    return sorted(keep_set, key=id_key), deleted


def write_status(repo, status):
    d = os.path.join(repo, "data", "control")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "status.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(status, indent=2, ensure_ascii=False) + "\n")


def read_status(repo):
    p = os.path.join(repo, "data", "control", "status.json")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _status(now, result, dataset_id=None, latest=None, summary=None, message="", live_verify=None):
    s = {"lastRun": er.utc_now_iso_ist(now), "result": result, "datasetId": dataset_id, "latestCompletedSession": latest, "summary": summary, "message": message}
    if live_verify is not None:
        s["liveVerify"] = live_verify
    return s


def fetch_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "dataops-verify", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def verify_live(base_url, dataset_id=None, fetch=fetch_bytes):
    """Fetch the live manifest (pointer, or snapshots/<id>/manifest.json) and every file it lists; sha256 over the RAW response bytes.
    -> {"ok": bool, "datasetId": ..., "checked": n, "failures": [...]}"""
    base = base_url if base_url.endswith("/") else base_url + "/"
    murl = base + ("data/snapshots/%s/manifest.json" % dataset_id if dataset_id else "data/manifest.json")
    failures, checked = [], 0
    try:
        m = json.loads(fetch(murl).decode("utf-8"))
    except Exception as e:
        return {"ok": False, "datasetId": dataset_id, "checked": 0, "failures": ["manifest %s: %s: %s" % (murl, type(e).__name__, e)]}
    sbase = base + "data/" + m["snapshotBase"]
    items = [("calendar", m["calendar"]["path"], m["calendar"]["sha256"])] + [(k, f["path"], f["sha256"]) for k, f in m["files"].items()]
    for key, path, sha in items:
        checked += 1
        try:
            got = H.sha256_bytes(fetch(sbase + path))
        except Exception as e:
            failures.append("%s: %s: %s" % (key, type(e).__name__, e))
            continue
        if got != sha:
            failures.append("%s: sha256 %s... != manifest %s..." % (key, got[:12], sha[:12]))
    return {"ok": not failures, "datasetId": m["datasetId"], "checked": checked, "failures": failures}


def publish(repo=REPO, dataset_id=None, now=None, base_url=BASE_URL, verify_wait=90, fetch=fetch_bytes, sleep=time.sleep, push=True, live_verify=True, work_root=None):
    """Publish one snapshot. Returns the status dict (also written to data/control/status.json). Raises PublishRefused if nothing was published
    because the snapshot is UNPUBLISHABLE/missing; PublishFail after the status was recorded as FAIL."""
    work = work_root or os.path.join(repo, "work")
    snaps = os.path.join(repo, "data", "snapshots")
    sid = dataset_id or latest_work_snapshot(work)
    if not sid or not os.path.isdir(os.path.join(work, sid)):
        raise PublishRefused("no snapshot to publish in %s" % work)
    with open(os.path.join(work, sid, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("publishStatus") != "PUBLISHABLE":
        write_status(repo, _status(now, "UNPUBLISHABLE", sid, manifest.get("latestCompletedSession"), manifest.get("summary"), "refused: publishStatus is %s" % manifest.get("publishStatus")))
        raise PublishRefused("snapshot %s is %s — refusing to publish" % (sid, manifest.get("publishStatus")))
    latest, summary = manifest["latestCompletedSession"], manifest["summary"]

    def fail(msg):
        st = _status(now, "FAIL", sid, latest, summary, msg)
        write_status(repo, st)
        raise PublishFail(msg)

    try:
        git(repo, "pull", "--ff-only")                                                   # 1
    except PublishFail as e:
        fail(str(e))
    dest = os.path.join(snaps, sid)
    if os.path.exists(dest):                                                             # 2: never overwrite a published snapshot
        fail("data/snapshots/%s already exists — refusing to overwrite a published snapshot" % sid)
    os.makedirs(snaps, exist_ok=True)
    shutil.copytree(os.path.join(work, sid), dest)
    problems = ex.verify_snapshot(dest, manifest)
    if problems:
        shutil.rmtree(dest)
        fail("copied snapshot failed re-verification: " + "; ".join(problems))
    shutil.copyfile(os.path.join(dest, "manifest.json"), os.path.join(repo, "data", "manifest.json"))     # 3: byte copy
    kept, deleted = retention(snaps, os.path.join(repo, "config", "pinned_snapshots.json"))               # 4
    st = _status(now, "PUBLISHED", sid, latest, summary, "published %s (%s); retention kept %d, removed %d" % (sid, manifest["certification"], len(kept), len(deleted)), "PENDING" if live_verify else "SKIPPED")
    write_status(repo, st)                                                               # 5
    before = tree_hash(dest)
    git(repo, "add", "-A", "data", "config")                                             # 6
    msg = "data: %s %s\n\n%s\n" % (sid, manifest["certification"], TRAILER)
    git(repo, "commit", "-q", "-m", msg)
    if push:
        r = git(repo, "push", check=False)
        if r.returncode != 0:                                                            # 7
            try:
                git(repo, "fetch", "origin")
                branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                changed = [x for x in git(repo, "diff", "--name-only", "HEAD...origin/%s" % branch).stdout.split() if x]
                foreign = [x for x in changed if not x.replace("\\", "/").startswith("data/control/requests/")]
                if foreign:
                    fail("push rejected and the remote changed unexpected files: %s (commit kept locally)" % ", ".join(foreign[:5]))
                git(repo, "pull", "--rebase")
                if tree_hash(dest) != before:
                    fail("snapshot folder changed during rebase (commit kept locally)")
                git(repo, "push")
            except PublishFail as e:
                if "commit kept locally" not in str(e):
                    fail("push failed after rebase: %s (commit kept locally; the next run pushes it)" % e)
                raise
    if push and live_verify:                                                             # 8
        sleep(verify_wait)
        lv = verify_live(base_url, sid, fetch)
        st = _status(now, "PUBLISHED", sid, latest, summary, st["message"], "OK" if lv["ok"] else "FAIL: " + "; ".join(lv["failures"][:3]))
        write_status(repo, st)
        git(repo, "add", "-A", "data/control")
        if git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0:
            git(repo, "commit", "-q", "-m", "data: status %s live verify %s\n\n%s\n" % (sid, "ok" if lv["ok"] else "FAILED", TRAILER))
            git(repo, "push", check=False)
    return st
