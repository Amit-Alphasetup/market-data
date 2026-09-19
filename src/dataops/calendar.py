"""calendar.json — plan section 4.7.

Regular sessions for past years are the actual trading dates of the session-calendar index (NIFTY 50; the same source v2 and the
certifier use) minus special sessions, because EOD2 stores holidays only for the CURRENT year. Future days of the current year are
weekdays minus that year's EOD2 holidays. `verifiedThrough` = 31 Dec of the year whose holidays EOD2 has (never before latest).
specialSessions = dates with a bar that are weekends, holidays, or announced special sessions (real sessions, AD11).
"""
import json

from . import eod2_reader as er


def build_calendar(cal_dates, holidays, holidays_year, special_announced, latest, cutoff_ist, coverage_from):
    """cal_dates: sorted ISO dates with a bar in the session-calendar index. Returns the calendar dict (key order = section 4.7)."""
    hol = set(holidays)
    special = []
    regular_past = []
    for d in cal_dates:
        if d < coverage_from or d > latest:
            continue
        wk = er.weekday(d)
        desc = holidays.get(d)
        if wk >= 5 or d in hol or d in special_announced:
            kind = "MUHURAT" if (desc and "Laxmi Pujan" in desc) else "SPECIAL"
            special.append({"date": d, "kind": kind})
        else:
            regular_past.append(d)
    year_latest = int(latest[:4])
    cov_year = holidays_year if (isinstance(holidays_year, int) and holidays_year >= year_latest) else year_latest
    coverage_to = "%04d-12-31" % cov_year
    if isinstance(holidays_year, int) and holidays_year >= year_latest:
        verified = "%04d-12-31" % holidays_year
    else:
        verified = latest        # no holiday list for the latest year: only facts up to `latest` are verified
    future = []
    d = er.add_days(latest, 1)
    while d <= coverage_to:
        if er.weekday(d) < 5 and d not in hol:
            future.append(d)
        d = er.add_days(d, 1)
    return {
        "id": "nse-eq-" + latest,
        "timezone": "Asia/Kolkata",
        "cutoffIST": cutoff_ist,
        "coverageFrom": coverage_from,
        "coverageTo": coverage_to,
        "verifiedThrough": verified,
        "latestCompletedSession": latest,
        "holidays": sorted(hol),
        "sessions": regular_past + future,
        "specialSessions": special,
    }


def dumps(cal):
    """The exact bytes written and hashed."""
    return json.dumps(cal, separators=(",", ":"), ensure_ascii=False)


def validate_calendar(cal):
    errs = []
    if not isinstance(cal, dict):
        return ["calendar is not an object"]
    for k in ("id", "timezone", "cutoffIST", "coverageFrom", "coverageTo", "verifiedThrough", "latestCompletedSession", "holidays", "sessions", "specialSessions"):
        if k not in cal:
            errs.append("calendar missing " + k)
    if errs:
        return errs
    s = cal["sessions"]
    if not s:
        errs.append("calendar has no sessions")
    elif s != sorted(set(s)):
        errs.append("calendar sessions not strictly ascending")
    special_dates = {x["date"] for x in cal["specialSessions"]}
    if s and cal["latestCompletedSession"] not in s and cal["latestCompletedSession"] not in special_dates:
        errs.append("latestCompletedSession is neither a regular nor a special session")
    if cal["verifiedThrough"] < cal["latestCompletedSession"]:
        errs.append("verifiedThrough before latestCompletedSession")
    return errs
