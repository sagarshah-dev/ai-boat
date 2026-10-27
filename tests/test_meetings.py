"""Feature 4: meetings. Proposed first, applied only on yes; the time rules are code, not model."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.ai import meeting_tools
from app.config import get_settings
from app.core.formatting import fmt_date
from app.db.models import Meeting, PendingAction
from app.db.session import session_scope
from app.integrations.calendar import CalendarError, CalendarNotConfigured
from app.main import app
from app.services import meetings as svc
from tests.bot_helpers import BIREN, SAGAR, member_id
from tests.conftest import call, calls, say

IST = ZoneInfo("Asia/Kolkata")


def next_day(weekday: int, weeks_ahead: int = 0) -> date:
    today = datetime.now(IST).date()
    ahead = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7 * weeks_ahead)


SUN = next_day(6)
SUN_S = fmt_date(SUN)


class FakeCalendar:
    def __init__(self, configured=True):
        self.configured = configured
        self.created, self.updated, self.deleted = [], [], []
        self.fail = None

    def _maybe_fail(self):
        if not self.configured:
            raise CalendarNotConfigured("off")
        if self.fail:
            raise CalendarError(self.fail)

    def create(self, title, start, minutes, agenda="", by=""):
        self._maybe_fail()
        self.created.append((title, start, minutes))
        return f"ev{len(self.created)}"

    def update(self, event_id, title, start, minutes, agenda="", by=""):
        self._maybe_fail()
        self.updated.append((event_id, start, minutes))

    def delete(self, event_id):
        self._maybe_fail()
        self.deleted.append(event_id)


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(meeting_tools, "calendar_for", lambda settings: fake)
    return fake


def schedule(date_s=SUN_S, time="17:00", title="Farmhouse meeting", **extra):
    return call("schedule_meeting", title=title, date=date_s, time=time, **extra)


def meetings_in_db():
    with session_scope() as s:
        return list(s.scalars(select(Meeting).order_by(Meeting.id)))


def add_meeting(start: datetime, minutes=60, title="Existing", event="evX"):
    with session_scope() as s:
        s.add(Meeting(title=title, starts_at=start, duration_min=minutes, agenda="",
                      calendar_event_id=event, created_by=member_id(SAGAR), status="scheduled"))


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# ── Schedule ───────────────────────────────────────────────────────────────────────────────

def test_schedule_preview_then_yes(bot, cal):
    bot.llm.script(schedule(agenda="borewell"))
    reply = bot.send(SAGAR, "meeting sunday 5pm borewell mate")
    assert "📅 Schedule this meeting?" in reply
    assert f"Sun {SUN_S}, 5:00 PM – 6:00 PM" in reply and "Agenda: borewell" in reply
    assert bot.tg.sent[-1]["buttons"] == [[("✅ Confirm", "yes"), ("❌ Cancel", "no")]]
    assert meetings_in_db() == [] and cal.created == []

    reply = bot.send(SAGAR, "yes")
    assert reply.startswith(f"✅ Meeting #1 scheduled — Sun {SUN_S}, 5:00 PM – 6:00 PM")
    [m] = meetings_in_db()
    assert m.calendar_event_id == "ev1" and m.starts_at == at(SUN, 17)
    assert cal.created[0][1] == at(SUN, 17)


def test_invite_email_with_ics(bot, cal):
    settings = get_settings().model_copy(update=dict(
        smtp_host="h", smtp_user="u", smtp_password="p", mail_from="f@x", recipient_emails="a@x"))
    app.dependency_overrides[get_settings] = lambda: settings
    bot.llm.script(schedule())
    bot.send(SAGAR, "meeting sunday 5pm")
    assert "Invite emailed to 1 recipient" in bot.send(SAGAR, "yes")
    [email] = bot.mailer.sent
    name, data, mime = email.attachments[0]
    assert name == "ai-boat-meeting-1.ics" and mime == "text/calendar; method=REQUEST"
    assert b"METHOD:REQUEST" in data and b"DTSTART:" in data
    assert "do NOT also open the attachment" in email.body


@pytest.mark.parametrize("text", ["meeting rakho", "biren sathe vaat karvi che"])
def test_no_when_in_message_is_never_invented(bot, cal, text):
    bot.llm.script(schedule())
    reply = bot.send(SAGAR, text)
    assert "When? Send the day and time together" in reply
    assert meetings_in_db() == []


def test_missing_time_is_asked_and_completed(bot, cal):
    bot.llm.script(schedule(time=""))
    assert "what time?" in bot.send(SAGAR, "meeting sunday")
    bot.llm.script(schedule())
    reply = bot.send(SAGAR, "5pm")
    assert bot.llm.last.user_messages == ["meeting sunday 5pm"]
    assert "Schedule this meeting?" in reply


@pytest.mark.parametrize("said", ["sunday 5 vagye meeting", "meeting sunday at 7", "રવિવારે 5 વાગે meeting"])
def test_hour_without_am_pm_is_asked_not_guessed(bot, cal, said):
    bot.llm.script(schedule())
    reply = bot.send(SAGAR, said)
    assert "in the morning or the evening?" in reply
    assert meetings_in_db() == []
    bot.llm.script(schedule())
    reply = bot.send(SAGAR, "saanje")
    assert bot.llm.last.user_messages == [f"{said} (saanje)"]
    assert "Schedule this meeting?" in reply


@pytest.mark.parametrize("said", ["sunday 5 vagye saanje meeting", "sunday 5pm meeting",
                                  "meeting sunday 17:00", "ravivare savare 10 vagye"])
def test_hour_with_part_of_day_is_fine(bot, cal, said):
    bot.llm.script(schedule(time="10:00" if "savare" in said else "17:00"))
    assert "Schedule this meeting?" in bot.send(SAGAR, said)


def test_named_weekday_must_match_the_date(bot, cal):
    monday = fmt_date(next_day(0))
    bot.llm.script(schedule(date_s=monday), schedule())
    reply = bot.send(SAGAR, "meeting sunday 5pm")
    assert "Sun" in reply and "Schedule this meeting?" in reply
    assert "The user said Sun" in str(bot.llm.last.tool_results[0])


@pytest.mark.parametrize("date_s,why", [
    (fmt_date(datetime.now(IST).date() - timedelta(days=3)), "in the past"),
    (fmt_date(datetime.now(IST).date() + timedelta(days=400)), "more than a year"),
    ("2026-11-01", "not DD-MM-YYYY"),
])
def test_bad_dates_go_back_to_the_model(bot, cal, date_s, why):
    bot.llm.script(schedule(date_s=date_s), say("Which date do you mean?"))
    assert bot.send(SAGAR, "meeting on 5th at 5pm") == "Which date do you mean?"
    assert why in str(bot.llm.last.tool_results[0])


def test_clash_remembers_the_request_and_takes_a_new_time(bot, cal):
    add_meeting(at(SUN, 16, 30), 60, title="Site visit")
    bot.llm.script(schedule())
    reply = bot.send(SAGAR, "meeting sunday 5pm")
    assert "clashes with #1 Site visit" in reply and "Send another time" in reply
    bot.llm.script(schedule(time="18:00"))
    reply = bot.send(SAGAR, "6pm")
    assert "make it 6pm instead" in bot.llm.last.user_messages[0]
    assert "6:00 PM – 7:00 PM" in reply


def test_slot_taken_between_preview_and_yes(bot, cal):
    bot.llm.script(schedule())
    bot.send(SAGAR, "meeting sunday 5pm")
    add_meeting(at(SUN, 17), title="Biren booked it")
    assert "was booked at that time meanwhile" in bot.send(SAGAR, "yes")
    assert len(meetings_in_db()) == 1 and cal.created == []


def test_agenda_copied_into_title_is_cleaned(bot, cal):
    bot.llm.script(schedule(title="discuss borewell pump repair", agenda="discuss borewell pump repair"))
    assert "\nFarmhouse meeting\nAgenda:" in bot.send(SAGAR, "sunday 5pm discuss borewell pump repair")


def test_calendar_not_set_up_still_saves(bot, cal):
    cal.configured = False
    bot.llm.script(schedule())
    bot.send(SAGAR, "meeting sunday 5pm")
    assert "Google Calendar is not set up" in bot.send(SAGAR, "yes")
    assert meetings_in_db()[0].calendar_event_id is None


def test_calendar_failure_is_said(bot, cal):
    cal.fail = "500"
    bot.llm.script(schedule())
    bot.send(SAGAR, "meeting sunday 5pm")
    assert "could not be created. Add it by hand" in bot.send(SAGAR, "yes")
    assert len(meetings_in_db()) == 1


# ── Find, cancel, move ─────────────────────────────────────────────────────────────────────

def test_find_goes_back_to_model_then_cancel_preview(bot, cal):
    add_meeting(at(SUN, 17), title="Site visit")
    bot.llm.script(call("find_meetings"), call("cancel_meeting", meeting_id=1))
    reply = bot.send(SAGAR, "sunday ni meeting cancel karo")
    assert "Site visit" in str(bot.llm.last.tool_results[0])
    assert "🚫 Cancel meeting #1?" in reply
    reply = bot.send(SAGAR, "haa")
    assert "🚫 Meeting #1 cancelled by Sagar" in reply
    assert meetings_in_db()[0].status == "cancelled" and cal.deleted == ["evX"]


def test_find_answered_in_words(bot, cal):
    add_meeting(at(SUN, 17), title="Site visit")
    bot.llm.script(call("find_meetings"), say("Site visit is on Sunday at 5 PM."))
    assert bot.send(SAGAR, "koi meeting che?") == "Site visit is on Sunday at 5 PM."


def test_cancel_unknown_or_cancelled(bot, cal):
    add_meeting(at(SUN, 17))
    with session_scope() as s:
        s.get(Meeting, 1).status = "cancelled"
    bot.llm.script(call("cancel_meeting", meeting_id=1), say("That one is already cancelled."))
    assert bot.send(SAGAR, "cancel it") == "That one is already cancelled."
    bot.llm.script(call("cancel_meeting", meeting_id=99), say("No such meeting."))
    assert bot.send(SAGAR, "cancel 99") == "No such meeting."


def test_cancel_when_calendar_delete_fails(bot, cal):
    add_meeting(at(SUN, 17))
    cal.fail = "500"
    bot.llm.script(call("cancel_meeting", meeting_id=1))
    bot.send(SAGAR, "cancel meeting 1")
    assert "may still show in Google Calendar" in bot.send(SAGAR, "yes")
    assert meetings_in_db()[0].status == "cancelled"


def test_move_meeting(bot, cal):
    add_meeting(at(SUN, 17))
    bot.llm.script(call("reschedule_meeting", meeting_id=1, time="18:30"))
    reply = bot.send(BIREN, "make sunday's meeting 6:30 pm")
    assert "🔄 Move meeting #1?" in reply and "From: Sun" in reply and "6:30 PM" in reply
    reply = bot.send(BIREN, "yes")
    assert "moved" in reply and "(was Sun" in reply
    assert meetings_in_db()[0].starts_at == at(SUN, 18, 30) and cal.updated[0][0] == "evX"


def test_move_to_same_time_is_refused(bot, cal):
    add_meeting(at(SUN, 17))
    bot.llm.script(call("reschedule_meeting", meeting_id=1, time="17:00"), say("It is already at 5."))
    assert bot.send(SAGAR, "move it to 5pm sunday") == "It is already at 5."


def test_move_does_not_clash_with_itself(bot, cal):
    add_meeting(at(SUN, 17), 60)
    bot.llm.script(call("reschedule_meeting", meeting_id=1, time="17:30"))
    assert "Move meeting #1?" in bot.send(SAGAR, "push sunday meeting to 5:30pm")


def test_time_question_gives_up_after_three_tries(bot, cal):
    bot.llm.script(schedule(time=""))
    bot.send(SAGAR, "meeting sunday")
    for reply_text in ("99", "77"):
        bot.llm.script(schedule(time=""))
        assert "not a valid time" in bot.send(SAGAR, reply_text)
    bot.llm.script(schedule(time=""))
    assert "No valid time after 3 tries" in bot.send(SAGAR, "88")
    with session_scope() as s:
        assert s.scalar(select(PendingAction)) is None


def test_two_proposals_in_one_turn_keep_the_first(bot, cal):
    bot.llm.script(calls(("schedule_meeting", {"title": "A", "date": SUN_S, "time": "17:00"}),
                         ("schedule_meeting", {"title": "B", "date": SUN_S, "time": "19:00"})))
    reply = bot.send(SAGAR, "sunday 5pm meeting A and 7pm meeting B")
    assert "\nA" in reply and "\nB\n" not in reply


# ── Pure rules ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("17:00", "17:00"), ("17.00", "17:00"), ("5:30 pm", "17:30"), ("9 am", "09:00"),
    ("12 am", "00:00"), ("12 pm", "12:00"), ("5", None), ("25:00", None), ("13 pm", None), ("", None),
])
def test_parse_time(raw, expected):
    assert svc.parse_time(raw) == expected


@pytest.mark.parametrize("text,hour", [
    ("sunday 5 vagye", 5), ("at 7", 7), ("5 વાગે", 5), ("5 baje meeting", 5),
    ("saanje 5 vagye", None), ("5pm", None), ("17:00 vagye", None), ("at 7 pm", None),
    ("meeting sunday", None), ("5 nov meeting", None), ("savare 10 vagye", None),
])
def test_ambiguous_hour(text, hour):
    assert svc.ambiguous_hour(text) == hour


@pytest.mark.parametrize("text,day", [
    ("sunday", 6), ("ravivare", 6), ("સોમવારે", 0), ("monday or tuesday", None), ("money", None),
])
def test_weekday_named(text, day):
    assert svc.weekday_named(text) == day


@pytest.mark.parametrize("text,expected", [
    ("meeting rakho", False), ("kale saanje", True), ("aavtikale", True), ("5", True), ("sunny day", False),
])
def test_datetime_signal(text, expected):
    assert svc.datetime_signal(text) is expected


def test_ics_folding_and_cancel():
    start = datetime(2026, 11, 1, 17, tzinfo=IST)
    ics = meeting_tools.build_ics(3, "ખેતર " * 30, start, 60, "a, b; c", cancelled=True, sequence=2)
    assert "METHOD:CANCEL" in ics and "STATUS:CANCELLED" in ics and "VALARM" not in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert "DTSTART:20261101T113000Z" in ics and "a\\, b\\; c" in ics


def test_time_already_passed_today(bot, cal):
    now = datetime.now(IST)
    earlier = now - timedelta(hours=1)
    if earlier.date() != now.date():
        pytest.skip("too close to midnight to have an earlier hour today")
    bot.llm.script(call("schedule_meeting", title="x", date=fmt_date(now.date()),
                        time=earlier.strftime("%H:%M")), say("That time has passed today."))
    assert bot.send(SAGAR, f"meeting today {earlier:%H:%M}") == "That time has passed today."
    assert "has already passed" in str(bot.llm.last.tool_results[0])
