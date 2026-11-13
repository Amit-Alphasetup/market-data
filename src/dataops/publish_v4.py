"""Publisher v4 (Backtest Lab plan r2, decision A2): the whole v4 tree is ONE orphan commit, force-pushed to the `pub` branch of the nse-data
repo and served by GitHub Pages. Nothing accumulates: every publish replaces the branch. The tree is served under data/ so that the manifest
URL is <base>/data/manifest.json for both contracts.

    run_v4(eod2_dir, config, remote)   export -> verify -> publish;  SKIPPED when the published dataset already has the same content
    publish_tree(tree_dir, remote)     verify -> orphan commit -> force push

Refuses to publish a tree that fails verify_v4 or is not PUBLISHABLE. Never touches the market-data repo or the v3 snapshots.
"""
import json
import os
import shutil
import subprocess
import tempfile

from . import export_v4 as x4

REPO_NAME = "Amit-Alphasetup-nse-data"      # the repo Apd created (2026-11-12) carries this name, not the plan's "nse-data"
REMOTE = "https://Amit-Alphasetup@github.com/Amit-Alphasetup/%s.git" % REPO_NAME
BRANCH = "pub"
BASE_URL = "https://amit-alphasetup.github.io/%s/" % REPO_NAME
AUTHOR = ("Amit-Alphasetup", "apdash95@gmail.com")
TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
INDEX_HTML = "<!doctype html><meta charset=\"utf-8\"><title>nse-data</title><p>NSE end-of-day dataset (contract alphadesk-eod2/4). Start at <a href=\"data/manifest.json\">data/manifest.json</a>.</p>\n"


class PublishV4Refused(Exception):
    pass


def _git(cwd, *args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise PublishV4Refused("git %s failed: %s" % (" ".join(args[:2]), (r.stderr or r.stdout).strip()[:300]))
    return r


def published_manifest(remote=REMOTE, branch=BRANCH):
    """The manifest currently on the published branch, or None (branch missing / unreachable / not a v4 tree)."""
    tmp = tempfile.mkdtemp(prefix="v4pub_")
    try:
        _git(tmp, "init", "-q")
        if _git(tmp, "fetch", "-q", "--depth", "1", remote, branch, check=False).returncode != 0:
            return None
        r = _git(tmp, "show", "FETCH_HEAD:data/manifest.json", check=False)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def content_key(manifest):
    """Identity of the published CONTENT, independent of the revision number (the tail files carry the dataset id, so datasetHash does not
    serve): latest session, policy, every history hash and every symbol's bar count/last date."""
    return json.dumps([manifest["latestCompletedSession"], manifest["policyHash"], manifest["calendar"]["sha256"],
                       sorted((k, (f["hist"] or {}).get("sha256"), f["bars"], f["last"], f["status"]) for k, f in manifest["files"].items()),
                       sorted(manifest["excluded"]), sorted(manifest["quarantined"])], sort_keys=True)


def publish_tree(tree_dir, remote=REMOTE, branch=BRANCH, push=True):
    """-> {"datasetId", "commit", "files", "bytes"}. Raises PublishV4Refused."""
    mp = os.path.join(tree_dir, "manifest.json")
    if not os.path.isfile(mp):
        raise PublishV4Refused("no manifest.json in %s" % tree_dir)
    with open(mp, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("publishStatus") != "PUBLISHABLE":
        raise PublishV4Refused("tree is %s - refusing to publish" % manifest.get("publishStatus"))
    problems = x4.verify_v4(tree_dir, manifest)
    if problems:
        raise PublishV4Refused("tree failed verification: " + "; ".join(problems[:5]))
    tmp = tempfile.mkdtemp(prefix="v4pub_")
    try:
        shutil.copytree(tree_dir, os.path.join(tmp, "data"))
        with open(os.path.join(tmp, ".nojekyll"), "w") as fh:
            fh.write("")
        with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(INDEX_HTML)
        with open(os.path.join(tmp, ".gitattributes"), "w", newline="\n") as fh:
            fh.write("* -text\n")                                     # bytes are hashed: git must never touch line endings
        if x4.verify_v4(os.path.join(tmp, "data"), manifest):
            raise PublishV4Refused("copied tree failed verification")
        _git(tmp, "init", "-q")
        _git(tmp, "checkout", "-q", "--orphan", branch)
        _git(tmp, "config", "user.name", AUTHOR[0])
        _git(tmp, "config", "user.email", AUTHOR[1])
        _git(tmp, "config", "core.autocrlf", "false")
        _git(tmp, "add", "-A")
        _git(tmp, "commit", "-q", "-m", "data: v4 %s %s\n\n%s\n" % (manifest["datasetId"], manifest["certification"], TRAILER))
        commit = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        n = len(_git(tmp, "ls-files").stdout.split("\n")) - 1
        if push:
            exists = bool(_git(tmp, "ls-remote", "--heads", remote, branch, check=False).stdout.strip())
            # the first publish creates the branch with an ordinary push; only REPLACING the published tree needs the force
            _git(tmp, "push", "-q", *(["--force"] if exists else []), remote, "HEAD:refs/heads/" + branch)
        return {"datasetId": manifest["datasetId"], "commit": commit, "files": n, "bytes": manifest["summary"].get("bytes", 0)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_v4(eod2_dir, config_path, remote=REMOTE, branch=BRANCH, work=None, now=None, push=True):
    """Export + publish. -> {"result": "PUBLISHED"|"SKIPPED"|"REFUSED", ...}."""
    prev = published_manifest(remote, branch)
    work = work or tempfile.mkdtemp(prefix="v4work_")
    out = os.path.join(work, "tree")
    try:
        shutil.rmtree(out, ignore_errors=True)
        res = x4.build_v4(eod2_dir, config_path, out, now=now)
        if res.publish_status != "PUBLISHABLE":
            return {"result": "REFUSED", "message": res.message or "export is not publishable"}
        if prev and prev.get("contract") == x4.CONTRACT_V4 and prev.get("latestCompletedSession") == res.manifest["latestCompletedSession"]:
            if content_key(prev) == content_key(res.manifest):
                return {"result": "SKIPPED", "datasetId": prev["datasetId"], "message": "published dataset already has this content"}
            rev = int(str(prev["datasetId"]).rsplit("-r", 1)[1]) + 1                 # same session, different content: next revision
            shutil.rmtree(out, ignore_errors=True)
            res = x4.build_v4(eod2_dir, config_path, out, now=now, revision=rev)
        elif prev and prev.get("latestCompletedSession", "") > res.manifest["latestCompletedSession"]:
            return {"result": "REFUSED", "message": "published dataset (%s) is NEWER than this export (%s): the EOD2 copy used here is stale"
                    % (prev["latestCompletedSession"], res.manifest["latestCompletedSession"])}
        info = publish_tree(out, remote, branch, push=push)
        info.update({"result": "PUBLISHED", "summary": x4.format_summary(res)})
        return info
    finally:
        shutil.rmtree(out, ignore_errors=True)


def verify_live_v4(base_url=BASE_URL, symbols=10, fetch=None):
    """Fetch the live manifest and `symbols` series (history + tail), sha256 over the raw bytes. -> {"ok", "datasetId", "checked", "failures"}."""
    import urllib.parse
    import urllib.request
    from . import hashing as H

    def default_fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "dataops-verify", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    fetch = fetch or default_fetch
    base = base_url if base_url.endswith("/") else base_url + "/"
    enc = lambda p: "/".join(urllib.parse.quote(x, safe="") for x in p.split("/"))
    failures, checked = [], 0
    try:
        m = json.loads(fetch(base + "data/manifest.json").decode("utf-8"))
    except Exception as e:
        return {"ok": False, "datasetId": None, "checked": 0, "failures": ["manifest: %s: %s" % (type(e).__name__, e)]}
    items = [("calendar", m["calendar"]["path"], m["calendar"]["sha256"])] + [("tail " + k, t["path"], t["sha256"]) for k, t in m["tails"].items()]
    keys = sorted(k for k, f in m["files"].items() if f["hist"])
    step = max(1, len(keys) // max(1, symbols)) if symbols else 1
    pick = keys if not symbols else keys[::step][:symbols]
    items += [(k, m["files"][k]["hist"]["path"], m["files"][k]["hist"]["sha256"]) for k in pick]
    for label, path, sha in items:
        checked += 1
        try:
            got = H.sha256_bytes(fetch(base + "data/" + enc(path)))
        except Exception as e:
            failures.append("%s: %s: %s" % (label, type(e).__name__, e))
            continue
        if got != sha:
            failures.append("%s: sha256 %s... != manifest %s..." % (label, got[:12], sha[:12]))
    return {"ok": not failures, "datasetId": m.get("datasetId"), "checked": checked, "failures": failures}
