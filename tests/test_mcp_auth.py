"""MCP sign-in (OAuth 2.1): the happy path Claude takes, and every way it must refuse.

Each refusal below is an attack or a mistake that would otherwise expose money data:
an unregistered redirect, a stolen code replayed, a rotated refresh token reused, a guessed
password, a request addressed to another host name."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from app.config import Settings
from app.db.models import OAuthLoginFailure, OAuthToken
from app.db.session import session_scope
from app.main import app
from app.mcp.auth_provider import allowed_redirect, digest
from tests.conftest import MCP_PASSWORD
from tests.mcp_helpers import BASE, CLAUDE_CB, RESOURCE, Claude, query


@pytest.fixture
def claude(bot) -> Claude:
    return Claude(bot.client)


def _expire(kind: str):
    with session_scope() as s:
        s.execute(update(OAuthToken).where(OAuthToken.kind == kind).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))


# ── Discovery: what Claude reads before anything else ──────────────────────────────────────

def test_no_token_gets_401_pointing_at_the_metadata(claude):
    r = claude.rpc("initialize", {}, token=None)
    assert r.status_code == 401
    # Claude starts sign-in only from a 401 carrying this pointer (Claude connector docs).
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]


def test_metadata_names_this_server_exactly(claude):
    prm = claude.c.get(BASE + "/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE                  # must equal the URL typed into Claude
    assert prm["authorization_servers"] == [BASE]
    asm = claude.c.get(BASE + "/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == BASE
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"] == BASE + "/register"
    assert asm["authorization_endpoint"] == BASE + "/authorize"
    assert asm["scopes_supported"] == ["read"]


# ── Registration (DCR) ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("uri, ok", [
    (CLAUDE_CB, True),
    ("http://localhost:3118/callback", True),           # Claude Code, any port
    ("http://127.0.0.1:50211/callback", True),
    ("http://localhost/callback", True),
    ("https://evil.example/callback", False),
    ("https://claude.ai.evil.example/api/mcp/auth_callback", False),
    ("https://claude.ai/api/mcp/auth_callback/x", False),
    ("http://claude.ai/api/mcp/auth_callback", False),   # not https
    ("http://localhost:3118/other", False),
    ("http://192.168.1.5:3118/callback", False),
])
def test_only_claude_redirects_may_register(claude, uri, ok):
    assert allowed_redirect(uri) is ok
    r = claude.register([uri])
    assert r.status_code == (201 if ok else 400)
    if not ok:
        assert r.json()["error"] == "invalid_redirect_uri"


def test_one_bad_redirect_in_the_list_rejects_the_registration(claude):
    assert claude.register([CLAUDE_CB, "https://evil.example/cb"]).status_code == 400


# ── The full sign-in ───────────────────────────────────────────────────────────────────────

def test_full_sign_in_then_tool_call(claude):
    assert claude.register().status_code == 201
    ticket = claude.ticket(state="abc")
    page = claude.c.get(BASE + "/oauth/login", params={"req": ticket})
    assert page.status_code == 200
    assert "<b>Claude</b> wants <b>read-only</b> access" in page.text
    assert "go back to: <b>claude.ai</b>" in page.text          # where the code will go
    assert page.headers["x-frame-options"] == "DENY"            # no clickjacking
    assert page.headers["referrer-policy"] == "no-referrer"     # the ticket never leaks
    assert page.headers["cache-control"] == "no-store"

    r = claude.login(ticket)
    assert r.status_code == 303
    back = r.headers["location"]
    assert back.startswith(CLAUDE_CB + "?")
    q = query(back)
    assert q["state"] == "abc" and len(q["code"]) >= 40

    tok = claude.exchange(q["code"])
    assert tok.status_code == 200
    t = tok.json()
    assert t["token_type"] == "Bearer" and t["expires_in"] == 3600 and t["scope"] == "read"
    assert t["refresh_token"]

    init = claude.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}}, t["access_token"])
    assert init.status_code == 200
    info = init.json()["result"]["serverInfo"]
    assert (info["name"], info["version"]) == ("ai-boat", "1.0.0")
    assert claude.tool(t["access_token"], "get_balance")["ok"] is True


def test_secrets_are_stored_only_as_hashes(claude):
    t = claude.sign_in()
    with session_scope() as s:
        rows = list(s.scalars(select(OAuthToken)))
        stored = {r.token_hash for r in rows}
        dump = " ".join(str(r.data) for r in rows)
    for secret in (t["access_token"], t["refresh_token"]):
        assert secret not in stored and digest(secret) in stored
        assert secret not in dump


def test_wrong_pkce_verifier_gets_no_token(claude):
    claude.register()
    code = claude.code()
    r = claude.exchange(code, verifier="x" * 50)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("challenge", ["", "short", "x" * 42, "a" * 43 + "!"])
def test_authorize_without_a_real_pkce_challenge_is_refused(claude, challenge):
    claude.register()
    r = claude.authorize(challenge=challenge)
    loc = r.headers.get("location", "")
    assert "/oauth/login" not in loc
    assert "error=invalid_request" in loc or r.status_code == 400


def test_authorize_with_no_challenge_at_all_is_refused(claude):
    claude.register()
    r = claude.authorize(challenge=None)
    assert "/oauth/login" not in r.headers.get("location", "")


def test_authorize_with_unregistered_redirect_never_redirects_there(claude):
    claude.register()
    r = claude.authorize(redirect_uri="https://evil.example/callback")
    assert r.status_code == 400
    assert "evil.example" not in r.headers.get("location", "")


def test_unknown_scope_is_refused(claude):
    claude.register()
    r = claude.authorize(scope="write")
    assert "/oauth/login" not in r.headers.get("location", "")
    assert "invalid_scope" in (r.headers.get("location", "") + r.text)


# ── Codes and tokens work once ─────────────────────────────────────────────────────────────

def test_code_replay_is_refused_and_cancels_that_sign_in(claude):
    claude.register()
    code = claude.code()
    first = claude.exchange(code)
    assert first.status_code == 200
    again = claude.exchange(code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    # Someone else had the code: the tokens it produced are cancelled too.
    assert claude.rpc("tools/list", {}, first.json()["access_token"]).status_code == 401


def test_refresh_rotates_and_reuse_cancels_everything(claude):
    t1 = claude.sign_in()
    r = claude.refresh(t1["refresh_token"])
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"] and t2["access_token"] != t1["access_token"]
    assert claude.tool(t2["access_token"], "get_balance")["ok"]

    reused = claude.refresh(t1["refresh_token"])            # the old one, a stolen copy
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    assert claude.rpc("tools/list", {}, t2["access_token"]).status_code == 401
    assert claude.refresh(t2["refresh_token"]).status_code == 400


def test_expired_access_token_is_refused_but_refresh_still_works(claude):
    t = claude.sign_in()
    _expire("access")
    assert claude.rpc("tools/list", {}, t["access_token"]).status_code == 401
    r = claude.refresh(t["refresh_token"])
    assert r.status_code == 200
    assert claude.rpc("tools/list", {}, r.json()["access_token"]).status_code == 200


def test_expired_refresh_token_means_sign_in_again(claude):
    t = claude.sign_in()
    _expire("refresh")
    r = claude.refresh(t["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_expired_code_is_refused(claude):
    claude.register()
    code = claude.code()
    _expire("code")
    assert claude.exchange(code).status_code == 400


@pytest.mark.parametrize("token", ["garbage", "", "Bearer x"])
def test_made_up_tokens_are_refused(claude, token):
    claude.sign_in()
    assert claude.rpc("tools/list", {}, token or None).status_code == 401


def test_a_refresh_token_is_not_an_access_token(claude):
    t = claude.sign_in()
    assert claude.rpc("tools/list", {}, t["refresh_token"]).status_code == 401


def test_revoke_signs_out_the_whole_sign_in(claude):
    t = claude.sign_in()
    assert claude.revoke(t["access_token"]).status_code == 200
    assert claude.rpc("tools/list", {}, t["access_token"]).status_code == 401
    assert claude.refresh(t["refresh_token"]).status_code == 400


def test_two_sign_ins_are_independent(claude):
    a = claude.sign_in()
    b = claude.sign_in()
    claude.revoke(a["access_token"])
    assert claude.rpc("tools/list", {}, b["access_token"]).status_code == 200


def test_tokens_survive_a_restart(tg, llm, mailer, bot):
    """Cloud Run restarts the container whenever it likes; Claude must stay signed in."""
    t = Claude(bot.client).sign_in()
    with TestClient(app) as fresh:                  # a second start-up of the same app
        assert Claude(fresh).rpc("tools/list", {}, t["access_token"]).status_code == 200


# ── The login page ─────────────────────────────────────────────────────────────────────────

def test_wrong_password_counts_down_then_locks(claude):
    claude.register()
    ticket = claude.ticket()
    for left in (4, 3, 2, 1, 0):
        r = claude.login(ticket, "wrong-password")
        assert r.status_code == 401 and f"{left} tries left" in r.text
    locked = claude.login(ticket, MCP_PASSWORD)            # even the right one, while locked
    assert locked.status_code == 429 and "15 minutes" in locked.text
    with session_scope() as s:                              # 15 minutes later
        s.execute(update(OAuthLoginFailure).values(
            created_at=datetime.now(timezone.utc) - timedelta(minutes=16)))
    assert claude.login(ticket, MCP_PASSWORD).status_code == 303


def test_success_clears_the_failure_count(claude):
    claude.register()
    claude.login(claude.ticket(), "wrong-password")
    assert claude.login(claude.ticket(), MCP_PASSWORD).status_code == 303
    with session_scope() as s:
        assert s.scalars(select(OAuthLoginFailure)).first() is None


def test_empty_and_huge_passwords_are_just_wrong(claude):
    claude.register()
    t = claude.ticket()
    assert claude.login(t, "").status_code == 401
    assert claude.login(t, "x" * 10_000).status_code == 401
    assert claude.login(t, MCP_PASSWORD + "x").status_code == 401   # no prefix match


def test_cancel_sends_access_denied_back_and_spends_the_ticket(claude):
    claude.register()
    ticket = claude.ticket(state="s9")
    r = claude.login(ticket, "", action="deny")
    assert r.status_code == 303
    q = query(r.headers["location"])
    assert q == {"error": "access_denied", "state": "s9"}
    assert claude.login(ticket).status_code == 400                   # cannot allow afterwards


def test_double_submit_gives_one_code_only(claude):
    claude.register()
    ticket = claude.ticket()
    assert claude.login(ticket).status_code == 303
    again = claude.login(ticket)
    assert again.status_code == 400 and "Link expired" in again.text
    with session_scope() as s:
        assert len(list(s.scalars(select(OAuthToken).where(OAuthToken.kind == "code")))) == 1


@pytest.mark.parametrize("ticket", ["", "not-a-ticket"])
def test_unknown_ticket_shows_expired_page(claude, ticket):
    r = claude.c.get(BASE + "/oauth/login", params={"req": ticket} if ticket else None)
    assert r.status_code == 400 and "Connect" in r.text
    assert claude.login(ticket).status_code == 400


def test_old_ticket_shows_expired_page(claude):
    claude.register()
    ticket = claude.ticket()
    _expire("request")
    assert claude.c.get(BASE + "/oauth/login", params={"req": ticket}).status_code == 400
    assert claude.login(ticket).status_code == 400


def test_client_name_cannot_inject_html(claude):
    claude.register(name="<script>alert(1)</script>")
    page = claude.c.get(BASE + "/oauth/login", params={"req": claude.ticket()})
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text


# ── Transport guard and the rest of the service ────────────────────────────────────────────

def test_request_for_another_host_name_is_refused(claude):
    t = claude.sign_in()
    r = claude.rpc("tools/list", {}, t["access_token"], headers={"Host": "evil.example"})
    assert r.status_code == 421                       # DNS-rebinding guard


def test_bot_routes_still_win(claude):
    assert claude.c.get(BASE + "/health").json()["status"] == "ok"
    # The webhook answers with its own secret check, not with the MCP app's 404 / 401.
    r = claude.c.post(BASE + "/telegram/webhook", json={})
    assert r.status_code == 401 and "www-authenticate" not in r.headers


# ── Settings ───────────────────────────────────────────────────────────────────────────────

_REQ = dict(telegram_token="t" * 12, telegram_webhook_secret="s" * 16, database_url="postgresql://x/y",
            gemini_api_key="k", _env_file=None)


def test_mcp_is_off_unless_both_settings_are_set():
    assert Settings(**_REQ, public_base_url="", mcp_owner_password="").mcp_enabled is False


@pytest.mark.parametrize("url, pw", [
    ("https://boat.example", ""),                    # password missing
    ("", "a-long-enough-password"),                 # URL missing
    ("https://boat.example", "short"),              # too short to guard money data
    ("http://boat.example", "a-long-enough-password"),  # plain http on a real host
])
def test_half_configured_mcp_refuses_to_start(url, pw):
    with pytest.raises(ValidationError):
        Settings(**_REQ, public_base_url=url, mcp_owner_password=pw)


def test_localhost_http_is_allowed_for_local_runs():
    assert Settings(**_REQ, public_base_url="http://localhost:8080",
                    mcp_owner_password="a-long-enough-password").mcp_enabled


def test_two_racing_exchanges_of_one_code_cannot_both_win(claude):
    """Both requests load the code before either spends it (the race the UPDATE guards)."""
    import anyio
    from app.mcp.auth_provider import DbOAuthProvider
    from mcp.server.auth.provider import TokenError

    claude.register()
    code = claude.code()
    p = DbOAuthProvider(BASE, RESOURCE)

    async def race():
        client = await p.get_client(claude.client_id)
        a = await p.load_authorization_code(client, code)
        b = await p.load_authorization_code(client, code)
        assert a is not None and b is not None          # neither request has spent it yet
        await p.exchange_authorization_code(client, a)
        with pytest.raises(TokenError):
            await p.exchange_authorization_code(client, b)
    anyio.run(race)


def test_two_racing_refreshes_of_one_token_cannot_both_win(claude):
    import anyio
    from app.mcp.auth_provider import DbOAuthProvider
    from mcp.server.auth.provider import TokenError

    t = claude.sign_in()
    p = DbOAuthProvider(BASE, RESOURCE)

    async def race():
        client = await p.get_client(claude.client_id)
        a = await p.load_refresh_token(client, t["refresh_token"])
        b = await p.load_refresh_token(client, t["refresh_token"])
        await p.exchange_refresh_token(client, a, a.scopes)
        with pytest.raises(TokenError):
            await p.exchange_refresh_token(client, b, b.scopes)
    anyio.run(race)
