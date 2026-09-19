"""Human-readable export report (plan D1 step 11: the ETFs with WARN LISTING_DATE_UNVERIFIED for Q11)."""


def format_report(res):
    L = []
    if res.manifest is None:
        return "EXPORT: %s\n%s" % (res.publish_status, res.message)
    m = res.manifest
    s = m["summary"]
    L.append("EXPORT %s · %s · latest session %s · certification %s" % (m["datasetId"], m["publishStatus"], m["latestCompletedSession"], m["certification"]))
    L.append("requested %d · ok %d · warn %d · excluded %d · quarantined %d · datasetHash %s" % (
        s["requested"], s["ok"], s["warn"], s["excluded"], s["quarantined"], m["datasetHash"][:16]))
    if res.message:
        L.append("MESSAGE: " + res.message)
    if res.quarantined:
        L.append("QUARANTINED:")
        for k, q in res.quarantined.items():
            ev = q.get("events") or []
            L.append("  %-14s %s %s" % (k, q["code"], "; ".join("%s %+.1f%%" % (e["date"], e["ret"] * 100) if "ret" in e else str(e.get("detail")) for e in ev)))
    if res.excluded:
        L.append("EXCLUDED:")
        for k, x in res.excluded.items():
            L.append("  %-14s %s %s" % (k, x["code"], x.get("detail", "")))
    warn_codes = {}
    for k, f in res.files.items():
        for r in f["reasons"]:
            warn_codes.setdefault(r["code"], []).append(k)
    for code, syms in sorted(warn_codes.items()):
        L.append("WARN %s (%d): %s" % (code, len(syms), ", ".join(syms)))
    if res.warnings_listing:
        L.append("Q11 — ETFs with LISTING_DATE_UNVERIFIED (supply NSE listing date + evidence via `set-listing`): " + ", ".join(sorted(res.warnings_listing)))
    return "\n".join(L)
