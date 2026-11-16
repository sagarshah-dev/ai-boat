"""Drive the MCP server the way Claude does: discovery, registration, sign-in, tokens, tool calls."""
import base64
import hashlib
import itertools
import json
import secrets
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from tests.conftest import MCP_PASSWORD

BASE = "https://testserver"
RESOURCE = BASE + "/mcp"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
ACCEPT = "application/json, text/event-stream"
PROTOCOL = "2025-11-25"
_ids = itertools.count(1)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class Claude:
    """One OAuth client. `c` must be a client of the running app (lifespan started); every URL
    is absolute https://testserver/..., the URL the connector is configured with."""

    def __init__(self, c: TestClient):
        self.c = c
        self.client_id = ""
        self.verifier = ""

    def register(self, redirect_uris=(CLAUDE_CB,), name="Claude"):
        r = self.c.post(BASE + "/register", json={
            "redirect_uris": list(redirect_uris), "client_name": name,
            "token_endpoint_auth_method": "none", "response_types": ["code"],
            "grant_types": ["authorization_code", "refresh_token"]})
        if r.status_code == 201:
            self.client_id = r.json()["client_id"]
        return r

    def authorize(self, state="st-1", scope="read", challenge="auto", redirect_uri=CLAUDE_CB, **extra):
        self.verifier, auto = pkce()
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
                  "code_challenge": auto if challenge == "auto" else challenge,
                  "code_challenge_method": "S256", "state": state, "scope": scope,
                  "resource": RESOURCE, **extra}
        return self.c.get(BASE + "/authorize", params={k: v for k, v in params.items() if v is not None},
                          follow_redirects=False)

    def ticket(self, **kw) -> str:
        r = self.authorize(**kw)
        assert r.status_code == 302, r.text
        loc = r.headers["location"]
        assert loc.startswith(BASE + "/oauth/login?req=")
        return query(loc)["req"]

    def login(self, ticket: str, password: str = MCP_PASSWORD, action: str = "allow"):
        return self.c.post(BASE + "/oauth/login", data={"req": ticket, "password": password, "action": action},
                           follow_redirects=False)

    def code(self, **kw) -> str:
        r = self.login(self.ticket(**kw))
        assert r.status_code == 303, r.text
        return query(r.headers["location"])["code"]

    def exchange(self, code: str, verifier: str | None = None):
        return self.c.post(BASE + "/token", data={
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier if verifier is not None else self.verifier,
            "client_id": self.client_id, "redirect_uri": CLAUDE_CB, "resource": RESOURCE})

    def refresh(self, refresh_token: str):
        return self.c.post(BASE + "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                           "client_id": self.client_id, "resource": RESOURCE})

    def revoke(self, token: str):
        # A public client has no secret; the SDK's form still wants the field present.
        return self.c.post(BASE + "/revoke", data={"token": token, "client_id": self.client_id,
                                                   "client_secret": ""})

    def sign_in(self) -> dict:
        if not self.client_id:
            assert self.register().status_code == 201
        r = self.exchange(self.code())
        assert r.status_code == 200, r.text
        return r.json()

    # ── MCP (JSON-RPC over Streamable HTTP) ────────────────────────────────────────────────

    def rpc(self, method: str, params: dict | None, token: str | None, headers: dict | None = None):
        h = {"Accept": ACCEPT, "MCP-Protocol-Version": PROTOCOL, **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return self.c.post(BASE + "/mcp", headers=h, json={"jsonrpc": "2.0", "id": next(_ids),
                                                    "method": method, "params": params or {}})

    def tool(self, token: str, name: str, **args) -> dict:
        """The structured result, plus is_error, of one tool call; asserts the transport worked."""
        r = self.rpc("tools/call", {"name": name, "arguments": args}, token)
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        out = dict(result["structuredContent"])
        out["_is_error"] = result["isError"]
        # The text copy must say exactly the same as the structured copy.
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return out

