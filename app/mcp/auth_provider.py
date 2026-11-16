"""The OAuth 2.1 authorization server behind the MCP endpoint, kept in Postgres.

The SDK's routes do the protocol work: /register, /authorize, /token, /revoke, the metadata
documents, PKCE checking, code expiry, redirect URI matching. This class only stores and loads.
The sign-in itself, where Sagar types his password, happens on the login page (app/mcp/login.py).

Rules kept here, not in the SDK:
- Only Claude's callback (and Claude Code's loopback) may register. Without this, anyone could
  register an app with their own redirect URL and send Sagar a sign-in link that delivers his
  code to them.
- Secrets are stored as SHA-256 only. A leaked row cannot be used as a token.
- A code or refresh token works once. A second use is treated as theft, and every token from
  that sign-in is cancelled (OAuth 2.1 refresh-token rotation with reuse detection).
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

import anyio.to_thread
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.db.models import OAuthClient, OAuthToken
from app.db.session import session_scope
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken as TokenResponse

SCOPE = "read"
LOGIN_PATH = "/oauth/login"

REQUEST_TTL = timedelta(minutes=10)      # time to type the password
CODE_TTL = timedelta(minutes=5)          # code -> token happens within seconds; 5 min is slack
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)         # sign in again once a month at most

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
# Claude Code signs in through a browser that returns to a local port chosen per session.
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_LOOPBACK = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d{1,5})?/callback$")


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)         # 256 bits; the spec asks for at least 128


def _now() -> datetime:
    return datetime.now(timezone.utc)


def allowed_redirect(uri: str) -> bool:
    return uri == CLAUDE_CALLBACK or bool(_LOOPBACK.match(uri))


def live(session: Session, secret: str, kind: str) -> OAuthToken | None:
    row = session.get(OAuthToken, digest(secret))
    if row is None or row.kind != kind or row.expires_at <= _now():
        return None
    return row


def revoke_family(session: Session, family: str) -> None:
    session.execute(delete(OAuthToken).where(OAuthToken.family == family))


def _purge_expired(session: Session) -> None:
    """No scheduled job: old rows are swept whenever new ones are written."""
    session.execute(delete(OAuthToken).where(OAuthToken.expires_at <= _now()))


def _store(session: Session, kind: str, client_id: str, family: str, data: dict,
           ttl: timedelta) -> str:
    secret = new_secret()
    session.add(OAuthToken(token_hash=digest(secret), kind=kind, client_id=client_id,
                           family=family, data=data, expires_at=_now() + ttl))
    return secret


def issue_request(session: Session, client_id: str, data: dict) -> str:
    """The login page's ticket. A fresh family starts here: one sign-in = one family."""
    _purge_expired(session)
    return _store(session, "request", client_id, new_secret(), data, REQUEST_TTL)


def issue_code(session: Session, ticket: OAuthToken, data: dict) -> str:
    return _store(session, "code", ticket.client_id, ticket.family, data, CODE_TTL)


def spend(session: Session, row: OAuthToken) -> bool:
    """Mark used in one UPDATE, so two requests racing with the same code cannot both win."""
    done = session.execute(update(OAuthToken).where(
        OAuthToken.token_hash == row.token_hash, OAuthToken.used_at.is_(None)
    ).values(used_at=_now()))
    return done.rowcount == 1


def _issue_pair(session: Session, client_id: str, family: str, scopes: list[str],
                resource: str) -> TokenResponse:
    _purge_expired(session)
    data = {"scopes": scopes, "resource": resource}
    access = _store(session, "access", client_id, family, data, ACCESS_TTL)
    refresh = _store(session, "refresh", client_id, family, data, REFRESH_TTL)
    return TokenResponse(access_token=access, expires_in=int(ACCESS_TTL.total_seconds()),
                         scope=" ".join(scopes), refresh_token=refresh)


class DbOAuthProvider:
    """OAuthAuthorizationServerProvider for the SDK. The database is synchronous (SQLAlchemy), so
    every method runs its work in a worker thread instead of blocking the event loop."""

    def __init__(self, base_url: str, resource_url: str):
        self.base_url = base_url.rstrip("/")
        self.resource_url = resource_url

    @staticmethod
    async def _run(fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    # ── Clients (Dynamic Client Registration) ───────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def work():
            with session_scope() as s:
                row = s.get(OAuthClient, client_id)
                return OAuthClientInformationFull.model_validate(row.info) if row else None
        return await self._run(work)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not allowed_redirect(u)]
        if not uris or bad:
            raise RegistrationError("invalid_redirect_uri",
                                    "This server accepts sign-in only from Claude.")

        def work():
            with session_scope() as s:
                s.add(OAuthClient(client_id=client_info.client_id,
                                  info=client_info.model_dump(mode="json", exclude_none=True)))
        await self._run(work)

    # ── Authorization ───────────────────────────────────────────────────────────────────────

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Not signed in yet: park the request and send the browser to the password page."""
        # The SDK accepts an empty challenge; RFC 7636 says 43-128 base64url characters.
        if not _CHALLENGE.match(params.code_challenge or ""):
            raise AuthorizeError("invalid_request", "code_challenge must be an S256 PKCE challenge")
        data = {"scopes": params.scopes or [SCOPE], "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "state": params.state, "resource": params.resource}

        def work():
            with session_scope() as s:
                return issue_request(s, client.client_id, data)
        ticket = await self._run(work)
        return f"{self.base_url}{LOGIN_PATH}?req={ticket}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        def work():
            with session_scope() as s:
                row = live(s, authorization_code, "code")
                if row is None or row.client_id != client.client_id:
                    return None
                if row.used_at is not None:
                    revoke_family(s, row.family)        # code replayed: cancel that sign-in
                    return None
                d = row.data
                return AuthorizationCode(
                    code=authorization_code, scopes=d["scopes"], client_id=row.client_id,
                    expires_at=row.expires_at.timestamp(), code_challenge=d["code_challenge"],
                    redirect_uri=d["redirect_uri"],
                    redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
                    resource=d.get("resource"), subject="owner")
        return await self._run(work)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> TokenResponse:
        def work():
            with session_scope() as s:
                row = live(s, authorization_code.code, "code")
                if row is None or not spend(s, row):
                    return None
                return _issue_pair(s, client.client_id, row.family, authorization_code.scopes,
                                   self.resource_url)
        tokens = await self._run(work)
        if tokens is None:      # raised out here: the SDK's frozen errors break inside `with`
            raise TokenError("invalid_grant", "authorization code already used")
        return tokens

    # ── Tokens ──────────────────────────────────────────────────────────────────────────────

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        def work():
            with session_scope() as s:
                row = live(s, refresh_token, "refresh")
                if row is None or row.client_id != client.client_id:
                    return None
                if row.used_at is not None:
                    revoke_family(s, row.family)        # rotated token used again: stolen copy
                    return None
                return RefreshToken(token=refresh_token, client_id=row.client_id,
                                    scopes=row.data["scopes"],
                                    expires_at=int(row.expires_at.timestamp()),
                                    resource=row.data.get("resource"), subject="owner")
        return await self._run(work)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> TokenResponse:
        def work():
            with session_scope() as s:
                row = live(s, refresh_token.token, "refresh")
                if row is None or not spend(s, row):
                    return None
                return _issue_pair(s, client.client_id, row.family, scopes, self.resource_url)
        tokens = await self._run(work)
        if tokens is None:
            raise TokenError("invalid_grant", "refresh token already used")
        return tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        def work():
            with session_scope() as s:
                row = live(s, token, "access")
                if row is None:
                    return None
                return AccessToken(token=token, client_id=row.client_id, scopes=row.data["scopes"],
                                   expires_at=int(row.expires_at.timestamp()),
                                   resource=row.data.get("resource"), subject="owner")
        return await self._run(work)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Signing out from one token signs out the whole sign-in (access + refresh)."""
        def work():
            with session_scope() as s:
                row = s.get(OAuthToken, digest(token.token))
                if row is not None:
                    revoke_family(s, row.family)
        await self._run(work)

