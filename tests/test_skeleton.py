"""D0 skeleton tests: repo structure (plan 4.2), git attributes/ignore, CONTRACT.md == plan section 4 verbatim."""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = r"C:\dev\ALPHADESK_MASTER_BUILD_PLAN_v2.2.md"


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as f:
        return f.read()


def test_structure_exists():
    for rel in ["index.html", "CONTRACT.md", ".gitattributes", ".gitignore", "src/dataops/__init__.py", "config", "tests",
                "data/control/requests", "data/control/inflight", "data/control/history", "data/snapshots", "clients",
                "logs", "secrets", "work"]:
        assert os.path.exists(os.path.join(ROOT, rel)), rel


def test_placeholder_index():
    html = read("index.html")
    assert "<title>market-data</title>" in html and "<p>market-data</p>" in html


def test_gitattributes_and_gitignore():
    ga = read(".gitattributes").splitlines()
    assert "* text=auto eol=lf" in ga and "*.json text eol=lf" in ga
    gi = read(".gitignore").splitlines()
    for pat in ["secrets/", "logs/", "work/", "__pycache__/", "*.pyc"]:
        assert pat in gi, pat


def test_ignored_dirs_are_really_ignored():
    for rel in ["secrets/x.json", "logs/x.log", "work/x/y.json", "src/dataops/__pycache__/a.pyc"]:
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
        assert r.returncode == 0, rel + " must be git-ignored"


def test_no_line_ending_crlf_in_text_files():
    for rel in ["CONTRACT.md", "index.html", ".gitattributes", ".gitignore", "src/dataops/__init__.py"]:
        with open(os.path.join(ROOT, rel), "rb") as f:
            assert b"\r\n" not in f.read(), rel


def test_contract_md_is_plan_section_4_verbatim():
    plan = open(PLAN, encoding="utf-8", newline="").read()
    a = plan.index("## 4. DATA CONTRACT v3")
    b = plan.index("## 5. TEST HARNESSES")
    section = re.sub(r"\n---\s*$", "", plan[a:b].rstrip()).rstrip() + "\n"
    assert read("CONTRACT.md").replace("\r\n", "\n") == section.replace("\r\n", "\n")
    for needle in ['Contract ID: `"alphadesk-eod2/3"`', "### 4.1 Instrument registry", "### 4.7 calendar.json", "ANOMALOUS_MOVE_UNRESOLVED"]:
        assert needle in read("CONTRACT.md")
