"""Temporary market-data-like repo with a local bare remote (tests never touch GitHub)."""
import json
import os
import shutil
import subprocess

from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 3, 10, 19, 41, 7, tzinfo=IST)


def sh(*args, cwd=None):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, "%s failed: %s%s" % (args, r.stdout, r.stderr)
    return r.stdout.strip()


def init_pair(tmp_path, name="repo"):
    """-> (repo_path, bare_path). The repo has config/ + data/control skeleton pushed to main."""
    bare = os.path.join(str(tmp_path), name + "_remote.git")
    repo = os.path.join(str(tmp_path), name)
    sh("git", "init", "--bare", "-b", "main", bare)
    sh("git", "clone", "-q", bare, repo)
    for k, v in (("user.name", "Amit-Alphasetup"), ("user.email", "test@example.com")):
        sh("git", "-C", repo, "config", k, v)
    for d in ("config", os.path.join("data", "control", "requests"), os.path.join("data", "snapshots"), "work", "logs"):
        os.makedirs(os.path.join(repo, d), exist_ok=True)
    open(os.path.join(repo, "config", "pinned_snapshots.json"), "w").write("[]\n")
    open(os.path.join(repo, "data", "control", "requests", ".gitkeep"), "w").write("")
    open(os.path.join(repo, ".gitignore"), "w").write("work/\nlogs/\nsecrets/\n")
    open(os.path.join(repo, ".gitattributes"), "w", newline="\n").write("* text=auto eol=lf\n*.json text eol=lf\n")     # same as the real repo
    sh("git", "-C", repo, "checkout", "-q", "-b", "main")
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "commit", "-q", "-m", "init")
    sh("git", "-C", repo, "push", "-q", "-u", "origin", "main")
    return repo, bare


def clone(tmp_path, bare, name):
    p = os.path.join(str(tmp_path), name)
    sh("git", "clone", "-q", bare, p)
    for k, v in (("user.name", "Other"), ("user.email", "o@example.com")):
        sh("git", "-C", p, "config", k, v)
    return p


def push_file(clone_path, rel, text="x\n", msg="other change"):
    subprocess.run(["git", "-C", clone_path, "pull", "-q", "--rebase"], capture_output=True, text=True)
    full = os.path.join(clone_path, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(text)
    sh("git", "-C", clone_path, "add", "-A")
    sh("git", "-C", clone_path, "commit", "-q", "-m", msg)
    sh("git", "-C", clone_path, "push", "-q", "origin", "main")


def small_registry_file(repo):
    """config/universe.json for a repo (3 ETFs + 2 indices, like tests/eod2_factory.World)."""
    from eod2_factory import make_registry
    p = os.path.join(repo, "config", "universe.json")
    json.dump(make_registry(), open(p, "w", encoding="utf-8"), indent=2)
    return p
