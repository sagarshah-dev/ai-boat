"""Google Calendar: the shared calendar the owners subscribe to. Nothing else talks to it.

Why a shared calendar and no invitations: a service account on a personal Gmail cannot invite
guests (that needs Google Workspace domain-wide delegation). So the bot writes into one shared
calendar everyone subscribes to once, and personal copies go out as email + .ics.

Setup, once: enable the Google Calendar API in the project; create a calendar; share it with
the service account's email ("Make changes to events"); put its Calendar ID in CALENDAR_ID and
the service account key JSON in GOOGLE_SERVICE_ACCOUNT_JSON.
"""
import json
import logging
from datetime import datetime, timedelta

import google.auth.transport.requests
from google.oauth2 import service_account

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
API = "https://www.googleapis.com/calendar/v3/calendars"
TIMEOUT_S = 10


class CalendarNotConfigured(RuntimeError):
    pass


class CalendarError(RuntimeError):
    pass


class GoogleCalendar:
    def __init__(self, calendar_id: str, tz: str, service_account_json: str, session=None):
        self.calendar_id = calendar_id.strip()
        self.tz = tz
        self._json = service_account_json.strip()
        self._session = session

    @property
    def configured(self) -> bool:
        return bool(self.calendar_id and self._json)

    def _http(self):
        if not self.configured:
            raise CalendarNotConfigured("CALENDAR_ID / GOOGLE_SERVICE_ACCOUNT_JSON not set")
        if self._session is None:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(self._json), scopes=SCOPES)
            self._session = google.auth.transport.requests.AuthorizedSession(creds)
        return self._session

    def _body(self, title: str, start: datetime, minutes: int, agenda: str, by: str) -> dict:
        end = start + timedelta(minutes=minutes)
        description = agenda or ""
        if by:
            description = (description + "\n\n" if description else "") + f"Scheduled by {by} via AI BOaT."
        # Local wall time + an explicit zone: an offset alone would shift meetings if the
        # server's zone and the owners' ever differed.
        fmt = "%Y-%m-%dT%H:%M:%S"
        return {"summary": title, "description": description,
                "start": {"dateTime": start.strftime(fmt), "timeZone": self.tz},
                "end": {"dateTime": end.strftime(fmt), "timeZone": self.tz}}

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        resp = self._http().request(method, url, json=body, timeout=TIMEOUT_S)
        if resp.status_code >= 400:
            raise CalendarError(f"{method} {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def _events(self) -> str:
        from urllib.parse import quote
        return f"{API}/{quote(self.calendar_id, safe='')}/events"

    def create(self, title: str, start: datetime, minutes: int, agenda: str = "", by: str = "") -> str:
        return self._call("POST", self._events(), self._body(title, start, minutes, agenda, by))["id"]

    def update(self, event_id: str, title: str, start: datetime, minutes: int,
               agenda: str = "", by: str = "") -> None:
        # Update in place, keeping the id: anyone who copied the event sees it move.
        self._call("PUT", f"{self._events()}/{event_id}", self._body(title, start, minutes, agenda, by))

    def delete(self, event_id: str) -> None:
        try:
            self._call("DELETE", f"{self._events()}/{event_id}")
        except CalendarError as exc:
            # Already removed by hand: the user's intent ("cancel this") is met either way.
            if " 404:" in str(exc) or " 410:" in str(exc):
                log.warning("calendar event already gone", extra={"fields": {"event": event_id}})
                return
            raise
