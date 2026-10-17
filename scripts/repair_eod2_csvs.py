import glob, os, re, sys, collections
root, mode = sys.argv[1], sys.argv[2]   # mode: dry | apply
CUTOFF = sys.argv[3] if len(sys.argv) > 3 else None  # drop rows dated > CUTOFF
DATE = re.compile(r"(?=\d{4}-\d{2}-\d{2},)")
stats = collections.Counter()
conflicts = []
samples = []
for sub in ("daily",):
    for f in glob.glob(os.path.join(root, sub, "*.csv")):
        raw = open(f, "rb").read()
        text = raw.decode("utf-8")
        crlf = "\r\n" in text
        lines = text.replace("\r\n", "\n").split("\n")
        had_final_nl = text.endswith("\n")
        out, changed = [], False
        seen = {}
        for l in lines:
            if not l.strip():
                continue
            parts = [p for p in DATE.split(l) if p]
            if len(parts) > 1:
                changed = True
                stats["fused_lines"] += 1
            for p in parts:
                key = p.split(",", 1)[0]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
                    if CUTOFF and key > CUTOFF:
                        changed = True
                        stats["rows_after_cutoff_dropped"] += 1
                        continue
                    if key in seen:
                        changed = True
                        if seen[key] == p:
                            stats["identical_dupes_removed"] += 1
                        else:
                            stats["conflicting_dupes"] += 1
                            conflicts.append((os.path.basename(f), key))
                            continue
                    else:
                        seen[key] = p
                out.append(p)
        if changed:
            stats["files_changed"] += 1
            if len(samples) < 3:
                samples.append(os.path.basename(f))
            if mode == "apply":
                nl = "\r\n" if crlf else "\n"
                data = nl.join(out) + (nl if had_final_nl else "")
                open(f, "wb").write(data.encode("utf-8"))
print(dict(stats))
print("conflicts:", conflicts[:20], len(conflicts))
print("samples:", samples)
