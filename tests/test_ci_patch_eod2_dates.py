"""D6 phase 3: scripts/ci_patch_eod2_dates.py - EOD2's catch-up must not stop at a past date that merely shares
today's day of the month (the 2026-10-22/23 shadow-run stalls)."""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ci_patch_eod2_dates as patcher  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST; the CI runner has no tz database

# The relevant part of EOD2 @ 5c8c8dd src/defs/dates.py (nextDate), as read from C:\dev\eod2.
DATES_PY = '''from datetime import datetime, timedelta, timezone
import logging
logger = logging.getLogger(__name__)
tz_IN = timezone(timedelta(hours=5, minutes=30))  # EOD2 uses ZoneInfo("Asia/Kolkata"); same offset, no DST


class Dates:
    def __init__(self, lastUpdate: str):
        self.dt = self.lastUpdate = datetime.fromisoformat(lastUpdate).astimezone(tz_IN)

    def nextDate(self):
        curTime = datetime.now(tz_IN)
        self.dt = self.dt + timedelta(1)

        if self.dt > curTime:
            logger.info("All Up To Date")
            return False

        if self.dt.day == curTime.day and curTime.hour < 16:
            logger.info("All Up To Date. Check again after 07:00PM for today's EOD data")
            return False

        return True
'''


def make_src(tmp_path, text=DATES_PY):
    (tmp_path / "defs").mkdir()
    (tmp_path / "defs" / "dates.py").write_text(text, encoding="utf-8")
    return tmp_path


def catch_up(src_dir, last_update, now):
    """Run the EOD2 loop `while dates.nextDate(): ...` with a frozen clock; return the dates it would download."""
    spec = importlib.util.spec_from_file_location("eod2_dates_under_test", src_dir / "defs" / "dates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    mod.datetime = FrozenDatetime
    d = mod.Dates(last_update)
    out = []
    while d.nextDate():
        out.append(d.dt.date().isoformat())
    return out


def test_unpatched_reproduces_the_stall(tmp_path):
    # the 2026-10-22 scheduled run: started 02:07 IST on 2026-10-23, mirror seeded at 2026-09-22
    got = catch_up(make_src(tmp_path), "2026-09-22T19:00:00+05:30", datetime(2026, 10, 23, 2, 7, tzinfo=IST))
    assert got == []   # stops at 2026-09-23: same day of the month as "today" (23)


def test_patched_catches_up_to_yesterday(tmp_path):
    src = make_src(tmp_path)
    assert patcher.main([str(src)]) == 0
    got = catch_up(src, "2026-09-22T19:00:00+05:30", datetime(2026, 10, 23, 2, 7, tzinfo=IST))
    assert got[0] == "2026-09-23" and got[-1] == "2026-10-22" and len(got) == 30
    # and on the day itself, before 16:00 IST, it still refuses today's not-yet-published data
    got = catch_up(src, "2026-10-22T19:00:00+05:30", datetime(2026, 10, 23, 10, 0, tzinfo=IST))
    assert got == []
    # (lastUpdate carries 19:00, so the next date is "in the future" until 19:00 IST - unchanged EOD2 behaviour)
    got = catch_up(src, "2026-10-22T19:00:00+05:30", datetime(2026, 10, 23, 20, 0, tzinfo=IST))
    assert got == ["2026-10-23"]


def test_idempotent_and_refuses_drifted_source(tmp_path, capsys):
    src = make_src(tmp_path)
    assert patcher.main([str(src)]) == 0
    assert patcher.main([str(src)]) == 0
    assert "already patched" in capsys.readouterr().out
    drifted = tmp_path / "drift"
    drifted.mkdir()
    make_src(drifted, DATES_PY.replace(patcher.OLD, "if True:"))
    assert patcher.main([str(drifted)]) == 1
    assert "drifted" in capsys.readouterr().err


def test_old_line_matches_the_real_eod2_checkout():
    real = r"C:\dev\eod2\src\defs\dates.py"   # read-only; absent on the CI runner
    if not os.path.exists(real):
        return
    assert open(real, encoding="utf-8").read().count(patcher.OLD) == 1
