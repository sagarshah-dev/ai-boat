"""The page where Sagar signs in when Claude connects (the "authorize" step of OAuth).

Flow: Claude opens /authorize -> the SDK checks the request and calls provider.authorize() ->
the browser lands here with ?req=<ticket> -> correct password -> one-time code -> back to Claude.

Only one person can sign in, with the password from MCP_OWNER_PASSWORD. Five wrong passwords
in 15 minutes lock the page for everyone: guessing is what a lock is for, and the only person
who could be locked out by mistake is the owner, who can wait 15 minutes.
"""
import hmac
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import anyio.to_thread
from sqlalchemy import delete, func, select
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.db.models import OAuthClient, OAuthLoginFailure
from app.db.session import session_scope
from app.mcp.auth_provider import live, spend, digest, issue_code
from mcp.server.auth.provider import construct_redirect_uri

MAX_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)
MAX_PASSWORD_LEN = 200

# The ticket is in the URL: never let it leak through Referer, caching or framing.
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}

_STYLE = ("body{font-family:system-ui,sans-serif;max-width:420px;margin:48px auto;padding:0 16px;"
          "color:#222;line-height:1.5}h1{font-size:22px}.box{border:1px solid #ddd;border-radius:10px;"
          "padding:20px}input{width:100%;padding:10px;font-size:16px;box-sizing:border-box;"
          "margin:8px 0 16px}button{padding:10px 18px;font-size:16px;border-radius:8px;"
          "border:1px solid #888;background:#fff;margin-right:8px}button.ok{background:#1f6feb;"
          "color:#fff;border-color:#1f6feb}.err{color:#b00020}.small{font-size:13px;color:#666}")


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>AI BOaT sign-in</title>"
           f"<style>{_STYLE}</style></head><body>{body}</body></html>")
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def _expired() -> HTMLResponse:
    return _page("<h1>Link expired</h1><p>This sign-in link is no longer valid. Go back to Claude "
                 "and click <b>Connect</b> again.</p>", 400)


def _form(ticket: str, client_name: str, redirect_uri: str, error: str = "",
          status: int = 200) -> HTMLResponse:
    host = urlparse(redirect_uri).netloc
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    # The return address is shown because a sign-in page must tell the user where the code goes.
    return _page(
        "<h1>AI BOaT</h1><div class='box'>"
        f"<p><b>{html.escape(client_name)}</b> wants <b>read-only</b> access to farm notes, "
        "expenses and balance.</p>"
        f"<p class='small'>After sign-in you go back to: <b>{html.escape(host)}</b></p>{err}"
        "<form method='post'>"
        f"<input type='hidden' name='req' value='{html.escape(ticket)}'>"
        "<label for='pw'>Owner password</label>"
        "<input id='pw' type='password' name='password' autocomplete='current-password' autofocus>"
        "<button class='ok' type='submit' name='action' value='allow'>Allow</button>"
        "<button type='submit' name='action' value='deny'>Cancel</button>"
        "</form></div>", status)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _failures(s) -> int:
    return s.scalar(select(func.count()).select_from(OAuthLoginFailure)
                    .where(OAuthLoginFailure.created_at > _now() - LOCK_WINDOW)) or 0


def _ticket_view(ticket: str):
    """(client name, redirect uri) for a live, unused ticket; None otherwise."""
    with session_scope() as s:
        row = live(s, ticket, "request")
        if row is None or row.used_at is not None:
            return None
        client = s.get(OAuthClient, row.client_id)
        name = (client.info.get("client_name") if client else None) or "An app"
        return name, row.data["redirect_uri"]


def password_ok(given: str, expected: str) -> bool:
    # Compare digests in constant time: the answer time must not reveal how much was right.
    return hmac.compare_digest(digest(given), digest(expected))


def _decide(ticket: str, password: str, action: str, expected: str) -> tuple[str, str]:
    """('redirect', url) | ('locked', '') | ('wrong', attempts_left) | ('expired', '')."""
    with session_scope() as s:
        row = live(s, ticket, "request")
        if row is None or row.used_at is not None:
            return "expired", ""
        d = row.data
        if action == "deny":
            if not spend(s, row):
                return "expired", ""
            return "redirect", construct_redirect_uri(d["redirect_uri"], error="access_denied",
                                                      state=d.get("state"))
        if _failures(s) >= MAX_FAILURES:
            return "locked", ""
        if not password_ok(password, expected):
            s.add(OAuthLoginFailure())
            s.flush()
            return "wrong", str(max(MAX_FAILURES - _failures(s), 0))
        if not spend(s, row):                  # the same form submitted twice
            return "expired", ""
        s.execute(delete(OAuthLoginFailure))
        code = issue_code(s, row, {k: d.get(k) for k in (
            "scopes", "code_challenge", "redirect_uri", "redirect_uri_provided_explicitly",
            "resource")})
        return "redirect", construct_redirect_uri(d["redirect_uri"], code=code, state=d.get("state"))


def make_handlers(owner_password: str):
    async def login_page(request: Request) -> Response:
        ticket = request.query_params.get("req", "")
        view = await anyio.to_thread.run_sync(_ticket_view, ticket) if ticket else None
        if view is None:
            return _expired()
        return _form(ticket, *view)

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        ticket = str(form.get("req") or "")
        password = str(form.get("password") or "")[:MAX_PASSWORD_LEN]
        action = "deny" if form.get("action") == "deny" else "allow"
        if not ticket:
            return _expired()
        outcome, extra = await anyio.to_thread.run_sync(_decide, ticket, password, action,
                                                        owner_password)
        if outcome == "redirect":
            # 303: the browser follows with GET, so the password is never re-posted.
            return RedirectResponse(extra, status_code=303, headers=_HEADERS)
        if outcome == "expired":
            return _expired()
        view = await anyio.to_thread.run_sync(_ticket_view, ticket)
        if view is None:
            return _expired()
        if outcome == "locked":
            return _form(ticket, *view, error="Too many wrong passwords. Try again in 15 minutes.",
                         status=429)
        return _form(ticket, *view, error=f"Wrong password. {extra} tries left before a 15-minute lock.",
                     status=401)

    return login_page, login_submit
