"""Telegram summary (plan D2). Secrets: secrets/telegram.json {botToken, chatId}. DECISIONS Q4: skipped — a missing file only logs
"no secrets" and does nothing else."""
import json
import os
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS = os.path.join(REPO, "secrets", "telegram.json")


def _log(msg, log_dir=None):
    d = log_dir or os.path.join(REPO, "logs")
    os.makedirs(d, exist_ok=True)
    from . import eod2_reader as er
    with open(os.path.join(d, "notify.log"), "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (er.utc_now_iso_ist(), msg))


def format_message(status, manifest=None):
    """Success: 3 lines like the plan. Failure: first line `⚠️ EOD2 FAIL — <reason>`."""
    if status["result"] in ("FAIL", "UNPUBLISHABLE"):
        return "⚠️ EOD2 FAIL — %s" % (status.get("message") or status["result"])
    s = status.get("summary") or {}
    lv = status.get("liveVerify")
    first = "EOD2 %s · %s · ok %d · warn %d · excl %d · quar %d" % (
        status.get("latestCompletedSession"), status["result"], s.get("ok", 0), s.get("warn", 0), s.get("excluded", 0), s.get("quarantined", 0))
    second = "dataset %s · live verify %s" % (status.get("datasetId"), "✓" if lv == "OK" else ("not run" if lv in (None, "SKIPPED", "PENDING") else "✗"))
    lines = [first, second]
    if manifest and manifest.get("quarantined"):
        lines.append("quarantined: " + ", ".join("%s (%s)" % (k, v["code"]) for k, v in manifest["quarantined"].items()))
    return "\n".join(lines)


def notify(status, manifest=None, secrets_path=SECRETS, opener=None, log_dir=None):
    """Send the summary. Returns 'SENT' | 'SKIPPED' | 'ERROR: ...'. Never raises (alerts must not break a data run)."""
    if not os.path.isfile(secrets_path):
        _log("telegram: no secrets (%s) - skipped" % secrets_path, log_dir)
        return "SKIPPED"
    token = ""
    try:
        with open(secrets_path, encoding="utf-8") as f:
            sec = json.load(f)
        token = sec["botToken"]
        text = format_message(status, manifest)
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                     data=json.dumps({"chat_id": sec["chatId"], "text": text}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with (opener or urllib.request.urlopen)(req, timeout=20) as r:
            r.read()
        _log("telegram: sent", log_dir)
        return "SENT"
    except Exception as e:
        _log("telegram: error %s: %s" % (type(e).__name__, str(e).replace(token, "***") if token else e), log_dir)
        return "ERROR: %s" % type(e).__name__
