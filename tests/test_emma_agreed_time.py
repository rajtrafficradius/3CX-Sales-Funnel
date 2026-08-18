"""Emma's agreed-time parser — pure-python unit tests, no DB / no network.

parse_agreed_time("Monday at 11 am", booked_at) must yield the NEXT occurrence of that
weekday/date relative to booked_at as a concrete Melbourne datetime (morning=10:00 when
no hour is given, arvo=14:00), and stay conservative (None) when there's no day cue or
no usable time. Includes the four LIVE queue rows (Liftronic / MyProperty / Industrial /
BatteryMate phrasings).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from funnel_agent.emma import parse_agreed_time

MEL = ZoneInfo("Australia/Melbourne")

# Anchor every case on a known call time: Wednesday 12 Aug 2026, 09:30 Melbourne.
BOOKED = datetime(2026, 8, 12, 9, 30, tzinfo=MEL)


def _p(text: str) -> datetime | None:
    return parse_agreed_time(text, BOOKED)


def _mel(*args) -> datetime:
    return datetime(*args, tzinfo=MEL)


# --- the four LIVE rows ------------------------------------------------------ #
def test_live_liftronic_monday_at_11_am():
    assert _p("Monday at 11 am") == _mel(2026, 8, 17, 11, 0)          # next Monday


def test_live_myproperty_friday_at_330_pm():
    assert _p("Friday at 3:30 pm") == _mel(2026, 8, 14, 15, 30)       # this Friday


def test_live_industrial_monday_900_am():
    assert _p("Monday 9:00 am") == _mel(2026, 8, 17, 9, 0)


def test_live_batterymate_friday_21_august_1100_am():
    # explicit date wins over the weekday word; "a.m." normalises to am
    assert _p("Friday 21 August, 11:00 a.m.") == _mel(2026, 8, 21, 11, 0)


# --- owner-spec phrasings ----------------------------------------------------- #
def test_tomorrow_morning_about_ten():
    assert _p("tomorrow morning about ten") == _mel(2026, 8, 13, 10, 0)


def test_weekday_with_ordinal_day_of_month():
    # "the 18th" is the authority; Tuesday is just how the prospect said it
    assert _p("Tuesday, the 18th at 12:00") == _mel(2026, 8, 18, 12, 0)


def test_tomorrow_morning_defaults_to_10():
    assert _p("tomorrow morning") == _mel(2026, 8, 13, 10, 0)


def test_arvo_defaults_to_14():
    assert _p("Monday arvo") == _mel(2026, 8, 17, 14, 0)


def test_friday_330_pm_no_at():
    assert _p("Friday 3:30 pm") == _mel(2026, 8, 14, 15, 30)


def test_next_weekday_said_on_same_weekday_rolls_a_week():
    # said on a Wednesday, "next Wednesday" must NOT mean today
    assert _p("next Wednesday at 2pm") == _mel(2026, 8, 19, 14, 0)


def test_bare_business_hour_lands_pm():
    assert _p("today at 4") == _mel(2026, 8, 12, 16, 0)               # 1–7 → pm


def test_weekday_afternoon():
    assert _p("Thursday afternoon") == _mel(2026, 8, 13, 14, 0)


def test_noon():
    assert _p("noon Friday") == _mel(2026, 8, 14, 12, 0)


def test_same_weekday_time_already_passed_rolls_a_week():
    # said Wednesday 09:30 — "Wednesday at 8am" can only be NEXT Wednesday
    assert _p("Wednesday at 8am") == _mel(2026, 8, 19, 8, 0)


def test_word_hour_with_meridiem():
    # live-queue phrasing: "Two PM, Monday."
    assert _p("Two PM, Monday.") == _mel(2026, 8, 17, 14, 0)


def test_word_oclock_with_curly_apostrophe_and_daypart():
    # live Feathers row: "three o’clock tomorrow afternoon"
    assert _p("three o’clock tomorrow afternoon") == _mel(2026, 8, 13, 15, 0)


def test_bare_ordinal_without_the():
    # live rows: "18th at 10 a.m." / "Friday 21st at 1 pm" — ordinal beats the weekday word
    assert _p("18th at 10 a.m.") == _mel(2026, 8, 18, 10, 0)
    assert _p("Friday 21st at 1 pm") == _mel(2026, 8, 21, 13, 0)


# --- conservatism: prefill must never guess ----------------------------------- #
def test_no_day_cue_returns_none():
    assert _p("sometime at 3 pm") is None


def test_no_time_returns_none():
    assert _p("Monday works for me") is None


def test_garbage_returns_none():
    assert _p("no idea mate, call me back") is None
    assert _p("") is None
    assert parse_agreed_time(None, BOOKED) is None


def test_result_is_melbourne_aware():
    out = _p("Monday at 11 am")
    assert out is not None and out.tzinfo is not None
    assert out.utcoffset() == _mel(2026, 8, 17, 11, 0).utcoffset()
