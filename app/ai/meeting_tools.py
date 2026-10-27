"""Meeting tools for the agent, and what happens after "yes".

Every meeting change is PROPOSED first and applied only after the user confirms: a wrong
expense is one undo in a list only the owners read; a wrong meeting is an entry on three
phones and an email to three inboxes. The blast radius decides who gets a confirmation step.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.ai import proposals
from app.config import Settings
from app.core.formatting import parse_date
from app.core.replies import Email, Out, Reply, yes_no
from app.integrations.calendar import CalendarNotConfigured, GoogleCalendar
from app.integrations.gemini import ToolSpec
from app.services import mail
from app.services import meetings as svc
from app.services.meetings import SlotProblem

if TYPE_CHECKING:
    from app.ai.agent import Context

log = logging.getLogger(__name__)

SCHEDULE, FIND, CANCEL, RESCHEDULE = "schedule_meeting", "find_meetings", "cancel_meeting", "reschedule_meeting"
NAMES = {SCHEDULE, FIND, CANCEL, RESCHEDULE}
# A listing the model needs to read before it can pick a meeting to cancel or move.
NEEDS_MODEL = {FIND}


def _s(type_: str, description: str = "", **extra) -> dict:
    out = {"type": type_, **extra}
    if description:
        out["description"] = description
    return out


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(SCHEDULE,
                 "PROPOSE a new meeting. Nothing is created — the user sees your proposal and it is "
                 "saved only after they reply yes. Use for 'meeting rakho', 'sunday 5 vagye madiye', "
                 "'site visit tomorrow evening'. Work out real dates from 'kale', 'next sunday', "
                 "'aavta ravivare' using today's date; give time as 24-hour HH:MM ('saanje 5' -> "
                 "17:00). Never invent a date or time: leave a field empty if the user did not say it.",
                 _s("OBJECT", properties={
                     "title": _s("STRING", "Short title, e.g. 'Farmhouse meeting', 'Site visit'. Not the agenda."),
                     "date": _s("STRING", "DD-MM-YYYY. Empty if not said."),
                     "time": _s("STRING", "Start, 24-hour HH:MM. Empty if not said."),
                     "duration_min": _s("INTEGER", "Minutes. 60 if not said."),
                     "agenda": _s("STRING", "What it is about, if said."),
                 }, required=["title"])),
        ToolSpec(FIND,
                 "List meetings (read-only). Use for 'when is the meeting', 'koi meeting che', and "
                 "ALWAYS before cancelling or moving one, to get its ID.",
                 _s("OBJECT", properties={
                     "scope": _s("STRING", "'upcoming' (default), 'past' or 'all'.",
                                 enum=["upcoming", "past", "all"]),
                     "date": _s("STRING", "Only this day, DD-MM-YYYY. Optional."),
                 })),
        ToolSpec(CANCEL,
                 "PROPOSE cancelling a meeting; the user must reply yes. Call find_meetings first "
                 "and pass its ID. If more than one could match, ask the user which.",
                 _s("OBJECT", properties={"meeting_id": _s("INTEGER", "ID from find_meetings.")},
                    required=["meeting_id"])),
        ToolSpec(RESCHEDULE,
                 "PROPOSE moving a meeting; the user must reply yes. Call find_meetings first. "
                 "Leave date or time empty to keep it.",
                 _s("OBJECT", properties={
                     "meeting_id": _s("INTEGER", "ID from find_meetings."),
                     "date": _s("STRING", "New date DD-MM-YYYY, or empty to keep."),
                     "time": _s("STRING", "New start HH:MM, or empty to keep."),
                     "duration_min": _s("INTEGER", "New length, or empty to keep."),
                 }, required=["meeting_id"])),
    ]


def calendar_for(settings: Settings | None) -> GoogleCalendar:
    s = settings
    return GoogleCalendar(s.calendar_id if s else "", s.timezone if s else "Asia/Kolkata",
                          s.google_service_account_json if s else "")


def _tz(ctx: "Context") -> str:
    return ctx.settings.timezone if ctx.settings else "Asia/Kolkata"


def _now(ctx: "Context") -> datetime:
    return ctx.now or datetime.now(ZoneInfo(_tz(ctx)))


def _local(ctx: "Context", dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo(_tz(ctx)))


def _title(args: dict) -> str:
    title = re.sub(r"\s+", " ", str(args.get("title") or "")).strip()
    agenda = str(args.get("agenda") or "").strip()
    # The model sometimes copies the agenda into the title: keep the title short.
    if not title or len(title) > 60 or (agenda and title.lower() == agenda.lower()):
        return "Farmhouse meeting"
    return title


def _problem(err: SlotProblem) -> dict:
    if err.kind == "model":
        return {"error": err.message}
    if err.kind == "ampm":
        return {"error": err.message, "needs_ampm": err.data["hour"]}
    return {"error": err.message, "needs_new_time": True, "conflict": err.message}


# ── Tools ───────────────────────────────────────────────────────────────────────────────────

def execute(ctx: "Context", name: str, args: dict, message: str) -> dict:
    s = ctx.session
    if name == SCHEDULE:
        if not svc.datetime_signal(message):
            return {"error": "The message does not say when.", "needs_datetime": True}
        date_str, time_str = str(args.get("date") or "").strip(), str(args.get("time") or "").strip()
        if not date_str:
            return {"error": "No date.", "needs_datetime": True}
        if not time_str:
            return {"error": "No time.", "needs_time": True}
        try:
            slot = svc.check_slot(s, date_str=date_str, time_str=time_str,
                                  duration=args.get("duration_min"), message=message,
                                  today=ctx.today, now=_now(ctx), tz=_tz(ctx))
        except SlotProblem as err:
            return _problem(err)
        return {"proposal": {"kind": "meeting_schedule", "title": _title(args),
                             "start": slot.start.isoformat(), "minutes": slot.duration_min,
                             "agenda": str(args.get("agenda") or "").strip()}}

    if name == FIND:
        scope = str(args.get("scope") or "upcoming").lower()
        on = None
        if args.get("date"):
            on = parse_date(args["date"])
            if on is None:
                return {"error": f"date '{args['date']}' is not DD-MM-YYYY."}
        found = svc.listing(s, scope if scope in ("upcoming", "past", "all") else "upcoming",
                            _now(ctx), on, _tz(ctx))
        return {"count": len(found), "meetings": [
            {"id": m.id, "title": m.title, "when": svc.pretty_when(_local(ctx, m.starts_at), m.duration_min),
             "agenda": m.agenda} for m in found[:20]]}

    m = svc.get(s, args.get("meeting_id"))
    if m is None:
        return {"error": f"No meeting with ID {args.get('meeting_id')}. Call find_meetings and use an ID from it."}
    if m.status == svc.CANCELLED:
        return {"error": f"Meeting #{m.id} is already cancelled."}
    when = svc.pretty_when(_local(ctx, m.starts_at), m.duration_min)

    if name == CANCEL:
        return {"proposal": {"kind": "meeting_cancel", "meeting_id": m.id, "title": m.title, "when": when}}

    if name == RESCHEDULE:
        if not svc.datetime_signal(message):
            return {"error": "The message does not say when to move it to. Ask the user."}
        local = _local(ctx, m.starts_at)
        date_str = str(args.get("date") or "").strip() or local.strftime("%d-%m-%Y")
        time_str = str(args.get("time") or "").strip() or local.strftime("%H:%M")
        try:
            slot = svc.check_slot(s, date_str=date_str, time_str=time_str,
                                  duration=args.get("duration_min") or m.duration_min,
                                  message=message, today=ctx.today, now=_now(ctx), tz=_tz(ctx),
                                  ignore_id=m.id)
        except SlotProblem as err:
            return _problem(err)
        if slot.start == m.starts_at and slot.duration_min == m.duration_min:
            return {"error": f"Meeting #{m.id} is already at that time. Nothing to change."}
        return {"proposal": {"kind": "meeting_move", "meeting_id": m.id, "title": m.title,
                             "start": slot.start.isoformat(), "minutes": slot.duration_min,
                             "old_when": when}}
    return {"error": f"Unknown tool: {name}"}


# ── Previews and apply ──────────────────────────────────────────────────────────────────────

def upcoming_text(ctx: "Context") -> str:
    """For the 📅 menu button: a fixed listing, no model call."""
    found = svc.listing(ctx.session, "upcoming", _now(ctx), None, _tz(ctx))
    if not found:
        return "📅 No upcoming meetings."
    return "📅 Upcoming meetings:\n" + "\n".join(
        f"#{m.id} — {svc.pretty_when(_local(ctx, m.starts_at), m.duration_min)} — {m.title}"
        for m in found[:10])


def _start(p: dict) -> datetime:
    return datetime.fromisoformat(p["start"])


def _schedule_preview(p: dict) -> str:
    agenda = f"\nAgenda: {p['agenda']}" if p.get("agenda") else ""
    return (f"📅 Schedule this meeting?\n{svc.pretty_when(_start(p), p['minutes'])}\n"
            f"{p['title']}{agenda}\n\nTap ✅ Confirm or ❌ Cancel (or reply yes / no).")


def _cancel_preview(p: dict) -> str:
    return (f"🚫 Cancel meeting #{p['meeting_id']}?\n{p['when']}\n{p['title']}\n\n"
            "Tap ✅ Confirm or ❌ Cancel (or reply yes / no).")


def _move_preview(p: dict) -> str:
    return (f"🔄 Move meeting #{p['meeting_id']}?\nFrom: {p['old_when']}\n"
            f"To:   {svc.pretty_when(_start(p), p['minutes'])}\n{p['title']}\n\n"
            "Tap ✅ Confirm or ❌ Cancel (or reply yes / no).")


def _apply_schedule(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    start = _start(p)
    # Checked again: in the minutes since the preview someone may have booked this slot.
    hits = svc.conflicts(ctx.session, start, p["minutes"])
    if hits:
        return [Reply(f"❌ Not scheduled: meeting #{hits[0].id} ({hits[0].title}) was booked at "
                      "that time meanwhile. Send another time.")]
    cal, event_id, warn = calendar_for(ctx.settings), None, ""
    try:
        event_id = cal.create(p["title"], start, p["minutes"], p.get("agenda", ""), ctx.sender.name)
    except CalendarNotConfigured:
        warn = "\n(Google Calendar is not set up, so it is saved in the bot only.)"
    except Exception:  # noqa: BLE001 — the meeting is still worth keeping; the gap is said
        log.exception("calendar create failed")
        warn = "\n⚠️ Saved, but the Google Calendar entry could not be created. Add it by hand."
    m = svc.add(ctx.session, title=p["title"], slot=svc.Slot(start, p["minutes"]),
                agenda=p.get("agenda", ""), by=ctx.sender, event_id=event_id)
    when = svc.pretty_when(start, p["minutes"])
    agenda = f"\nAgenda: {p['agenda']}" if p.get("agenda") else ""
    out: list[Out] = [Reply(f"✅ Meeting #{m.id} scheduled — {when}\n{m.title}{agenda}{warn}")]
    return out + _invite(ctx, "scheduled", m.id, m.title, start, p["minutes"], p.get("agenda", ""), when)


def _apply_cancel(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    m = svc.get(ctx.session, p["meeting_id"])
    if m is None or m.status == svc.CANCELLED:
        return [Reply("That meeting is already cancelled or no longer exists. Nothing was changed.")]
    warn = ""
    if m.calendar_event_id:
        try:
            calendar_for(ctx.settings).delete(m.calendar_event_id)
        except Exception:  # noqa: BLE001
            log.exception("calendar delete failed")
            warn = "\n⚠️ Cancelled here, but it may still show in Google Calendar — remove it by hand."
    m.status = svc.CANCELLED
    ctx.session.flush()
    start = _local(ctx, m.starts_at)
    when = svc.pretty_when(start, m.duration_min)
    out: list[Out] = [Reply(f"🚫 Meeting #{m.id} cancelled by {ctx.sender.name} — was {when}\n{m.title}{warn}")]
    return out + _invite(ctx, "cancelled", m.id, m.title, start, m.duration_min, m.agenda, when)


def _apply_move(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    m = svc.get(ctx.session, p["meeting_id"])
    if m is None or m.status == svc.CANCELLED:
        return [Reply("That meeting is cancelled or no longer exists. Nothing was changed.")]
    start = _start(p)
    hits = svc.conflicts(ctx.session, start, p["minutes"], ignore_id=m.id)
    if hits:
        return [Reply(f"❌ Not moved: meeting #{hits[0].id} ({hits[0].title}) was booked at that time meanwhile.")]
    old_when = svc.pretty_when(_local(ctx, m.starts_at), m.duration_min)
    warn = ""
    if m.calendar_event_id:
        try:
            calendar_for(ctx.settings).update(m.calendar_event_id, m.title, start, p["minutes"],
                                              m.agenda, ctx.sender.name)
        except Exception:  # noqa: BLE001
            log.exception("calendar update failed")
            warn = "\n⚠️ Moved here, but Google Calendar still shows the old time — fix it by hand."
    m.starts_at, m.duration_min, m.reminder_sent = start, p["minutes"], False
    ctx.session.flush()
    when = svc.pretty_when(start, p["minutes"])
    out: list[Out] = [Reply(f"🔄 Meeting #{m.id} moved — {when}\n{m.title}\n(was {old_when}){warn}")]
    return out + _invite(ctx, "rescheduled", m.id, m.title, start, p["minutes"], m.agenda, when, old_when)


proposals.register("meeting_schedule", proposals.Kind(_schedule_preview, _apply_schedule,
                                                      lambda p: yes_no("✅ Confirm")))
proposals.register("meeting_cancel", proposals.Kind(_cancel_preview, _apply_cancel,
                                                    lambda p: yes_no("✅ Confirm")))
proposals.register("meeting_move", proposals.Kind(_move_preview, _apply_move,
                                                  lambda p: yes_no("✅ Confirm")))


# ── Invite email with an .ics file ──────────────────────────────────────────────────────────

def _ics_text(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines over 75 bytes continue on the next line after a space; never split a
    multi-byte character (Gujarati titles)."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > (74 if out else 75):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def build_ics(meeting_id: int, title: str, start: datetime, minutes: int, agenda: str,
              *, cancelled: bool, sequence: int) -> str:
    def utc(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AI BOaT//Meetings//EN", "CALSCALE:GREGORIAN",
             f"METHOD:{'CANCEL' if cancelled else 'REQUEST'}", "BEGIN:VEVENT",
             f"UID:ai-boat-meeting-{meeting_id}@ai-boat", f"SEQUENCE:{sequence}",
             f"DTSTAMP:{utc(datetime.now(timezone.utc))}", f"DTSTART:{utc(start)}",
             f"DTEND:{utc(start + timedelta(minutes=minutes))}", f"SUMMARY:{_ics_text(title)}",
             f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}", "TRANSP:OPAQUE"]
    if agenda:
        lines.append(f"DESCRIPTION:{_ics_text(agenda)}")
    if not cancelled:
        lines += ["BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY",
                  "DESCRIPTION:Meeting in 1 hour", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


def _invite(ctx: "Context", action: str, mid: int, title: str, start: datetime, minutes: int,
            agenda: str, when: str, old_when: str = "") -> list[Out]:
    if not ctx.settings or not mail.ready(ctx.settings):
        return []
    cancelled = action == "cancelled"
    sequence = {"scheduled": 0, "rescheduled": 1, "cancelled": 2}[action]
    lines = [{"scheduled": "A new meeting has been scheduled.",
              "rescheduled": "A meeting has been moved.",
              "cancelled": "A meeting has been cancelled."}[action], "",
             f"What:  {title}", f"When:  {when}"]
    if old_when:
        lines.append(f"Was:   {old_when}")
    if agenda:
        lines.append(f"Agenda: {agenda}")
    lines += ["", f"{action.capitalize()} by {ctx.sender.name}."]
    if cancelled:
        lines += ["", "The attached file removes it from your calendar if you had added it."]
    else:
        end = start + timedelta(minutes=minutes)
        link = "https://calendar.google.com/calendar/render?" + urlencode({
            "action": "TEMPLATE", "text": title,
            "dates": f"{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}/{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
            **({"details": agenda} if agenda else {})})
        lines += ["", "It is already on the shared calendar. If you subscribe to that calendar,",
                  "do NOT also open the attachment — you would get it twice.",
                  "Not subscribed? Add it to your own calendar:",
                  "  - open the attached .ics file (any calendar app), or", f"  - {link}"]
    ics = build_ics(mid, title, start, minutes, agenda, cancelled=cancelled, sequence=sequence)
    prefix = {"scheduled": "Meeting", "rescheduled": "Updated", "cancelled": "Cancelled"}[action]
    return [Email(subject=f"{prefix}: {title} — {when}", body="\n".join(lines), to=ctx.settings.recipients,
                  attachments=((f"ai-boat-meeting-{mid}.ics", ics.encode(),
                                f"text/calendar; method={'CANCEL' if cancelled else 'REQUEST'}"),),
                  ok_note=f"📧 Invite emailed to {len(ctx.settings.recipients)} recipient(s).",
                  fail_note="⚠️ The meeting is saved, but the email invite could not be sent.")]

