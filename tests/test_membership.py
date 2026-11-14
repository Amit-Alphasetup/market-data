"""P6: point-in-time membership builder (scripts/build_membership.py) and its publication as ref/membership.json."""
import csv, json, os, sys, zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import build_membership as bm  # noqa: E402

from dataops import export_v4 as x4  # noqa: E402
from test_v4_export import build, load, walk, World, world  # noqa: E402

N50 = ["A%02d" % i for i in range(50)]
NX = ["B%02d" % i for i in range(50)]


def annex(title, syms, start=1):
    lines = [title + ",,,", ",,,", "Sr. No,Security Symbol,Security Name,Industry"]
    lines += ["%d,%s,%s Ltd.,X" % (start + i, s, s) for i, s in enumerate(syms)]
    return "\n".join(lines) + "\n"


def make_src(tmp, months, today50, todaynx, defect=None):
    """months: {"2020-01": (n50 syms, next50 syms)}. defect: month whose Next 50 file numbers rows from 2."""
    src = tmp / "src"
    (src / "nifty-mcwb").mkdir(parents=True)
    (src / "today").mkdir()
    for ym, (a, b) in months.items():
        name = "mcwb_%s%s.zip" % (bm.MONTHS[int(ym[5:]) - 1], ym[2:4])
        with zipfile.ZipFile(src / "nifty-mcwb" / name, "w") as z:
            z.writestr("nifty50_mcwb.csv", annex("Annexure II- Nifty 50", a))
            z.writestr("niftynext50_mcwb.csv", annex("Annexure II- Nifty Next 50", b, start=2 if ym == defect else 1))
    isin = lambda s: "INE" + s + "01019"
    for fn, syms in (("ind_nifty50list.csv", today50), ("ind_niftynext50list.csv", todaynx), ("ind_nifty100list.csv", today50 + todaynx)):
        with open(src / "today" / fn, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Company Name", "Industry", "Symbol", "Series", "ISIN Code"])
            for s in syms:
                w.writerow([s, "X", s, "EQ", isin(s)])
    allsyms = set(N50 + NX + ["C00", "C01"] + today50 + todaynx)
    m = {"sym2isin": {s: isin(s) for s in allsyms}, "isin2hist": {isin(s): [{"symbol": s, "from_date": "2011-01-01", "to_date": "2030-01-01"}] for s in allsyms}}
    mp = tmp / "map.json"
    mp.write_text(json.dumps(m))
    return src, mp


def run(tmp, src, mp):
    out = tmp / "membership.json"
    rc = bm.main(["--src", str(src), "--isin-map", str(mp), "--out", str(out)])
    return rc, json.loads(out.read_text())


def iv(doc, idx, sym):
    return [r for r in doc["indices"][idx]["intervals"] if r["symbol"] == sym]


def test_intervals_gaps_defects_placeholders_and_checks(tmp_path):
    n50_b = N50[:-1] + ["C00"]               # A49 leaves, C00 joins in 2020-03 (after the missing 2020-02 file)
    nx_b = NX[:-1] + ["C01"]                 # B49 -> C01 in 2020-04; the 2020-04 Next 50 file is defective
    months = {"2020-01": (N50, NX), "2020-03": (n50_b + ["DUMMYX"], NX), "2020-04": (n50_b, nx_b), "2020-05": (n50_b, nx_b)}
    src, mp = make_src(tmp_path, months, n50_b, nx_b, defect="2020-04")
    rc, doc = run(tmp_path, src, mp)
    assert rc == 0
    assert doc["missingMonths"] == ["2020-02"] and "DUMMYX" in doc["placeholdersDropped"]
    assert any("mcwb_apr20.zip NIFTY NEXT 50" == k for k in doc["defectiveFiles"])
    a49, c00 = iv(doc, "NIFTY 50", "A49"), iv(doc, "NIFTY 50", "C00")
    assert a49[0]["from"] == "2020-01-01" and a49[0]["to"] == "2020-02-29" and "no file for 2020-02" in a49[0]["evidence"]["removed"]
    assert c00[0]["from"] == "2020-03-01" and c00[0]["to"] is None
    # defective Apr file skipped for Next 50: the change is dated at May
    assert iv(doc, "NIFTY NEXT 50", "C01")[0]["from"] == "2020-05-01" and iv(doc, "NIFTY NEXT 50", "B49")[0]["to"] == "2020-04-30"
    for idx in ("NIFTY 50", "NIFTY NEXT 50", "NIFTY 100"):
        e = doc["indices"][idx]
        assert e["startDate"] == "2020-01-01" and e["checks"]["walkForwardEqualsToday"] and e["checks"]["intervalsWithoutEvidence"] == 0
        assert all(c == e["size"] for c in e["checks"]["sampleCounts"].values())
        assert all(r["isin"].startswith("INE") for r in e["intervals"])
    assert bm.members_on(doc["indices"]["NIFTY 100"]["intervals"], "2020-03-15") == {"INE%s01019" % s for s in n50_b + NX}


def test_walk_forward_mismatch_fails_the_index(tmp_path):
    src, mp = make_src(tmp_path, {"2020-01": (N50, NX), "2020-02": (N50, NX)}, N50[:-1] + ["C00"], NX)
    rc, doc = run(tmp_path, src, mp)
    e = doc["indices"]["NIFTY 50"]
    assert e["startDate"] is None and e["intervals"] == [] and not e["checks"]["walkForwardEqualsToday"]
    assert e["checks"]["onlyInTodayList"] == ["INEC0001019"] and doc["indices"]["NIFTY NEXT 50"]["startDate"] == "2020-01-01" and rc == 0


def test_count_mismatch_moves_the_start_date(tmp_path):
    months = {"2020-01": (N50[:-1], NX), "2020-02": (N50, NX), "2020-03": (N50, NX)}
    src, mp = make_src(tmp_path, months, N50, NX)
    _, doc = run(tmp_path, src, mp)
    assert doc["indices"]["NIFTY 50"]["startDate"] == "2020-02-01" and "2020-01" in doc["indices"]["NIFTY 50"]["checks"]["countMismatchMonths"]


def test_exporter_publishes_membership_and_verifies_it(tmp_path):
    w, root, cfg = world(tmp_path, {"RELI": walk(World(tmp_path).dates, 1200, phase=2)})
    res0 = build(tmp_path, root, cfg, name="no_ref")
    assert res0.manifest["ref"] == {}
    body = b'{"schema":"membership/1","indices":{}}\n'
    with open(os.path.join(os.path.dirname(cfg), "membership.json"), "wb") as f:
        f.write(body)
    res = build(tmp_path, root, cfg)
    r = res.manifest["ref"]["membership"]
    assert res.publish_status == "PUBLISHABLE" and r["path"] == "ref/membership.json" and load(res, r["path"]) == body and r["bytes"] == len(body)
    assert res.manifest["datasetHash"] == res0.manifest["datasetHash"]   # membership does not change the price dataset identity
    with open(os.path.join(res.out_dir, "ref", "membership.json"), "ab") as f:
        f.write(b" ")
    assert any("HASH_MISMATCH ref membership" in p for p in x4.verify_v4(res.out_dir, res.manifest))
