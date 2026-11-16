"""The MCP server: four read-only tools at /mcp, behind OAuth, in the same service as the bot.

Same service, not a second one: one deploy, one database pool, and the tools call the exact
services the bot uses. Stateless Streamable HTTP with JSON responses, because Cloud Run may
restart or replace the instance between two calls; nothing about a client lives in memory.
"""
import json
import logging
from datetime import datetime
from typing import Annotated, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import Field
from starlette.applications import Starlette

from app.config import Settings
from app.db.session import session_scope
from app.mcp import tools
from app.mcp.auth_provider import LOGIN_PATH, SCOPE, DbOAuthProvider
from app.mcp.login import make_handlers
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"

INSTRUCTIONS = (
    "Read-only data of a family farmhouse shared by three co-owners: the farm journal (work done "
    "on plants, sprays, results, the farmer's advice) and the shared expenses. Dates are "
    "DD-MM-YYYY. Money is Indian rupees. When talking about balances say 'pending' and 'gets "
    "back'; never 'owe'. Answer farm questions only from the notes returned, and cite their ids "
    "(J-12, K-3#1). Nothing here can add, change or delete data; that is done in the Telegram bot."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                             open_world_hint=False)

Text = Annotated[str, Field(description="Leave empty for no filter.")]
DateArg = Annotated[str, Field(description="DD-MM-YYYY, e.g. 01-10-2026. Empty = no limit.")]


def _result(payload: dict) -> CallToolResult:
    # Text for clients that read only text, structured for those that parse; same JSON in both.
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                          structured_content=payload, is_error=not payload.get("ok", False))


def _call(settings: Settings, get_llm: Callable, name: str, fn: Callable, **args) -> CallToolResult:
    """One place for the error contract: a bad argument and our own failure look the same to
    every tool's caller, and a stack trace never reaches the client."""
    try:
        with session_scope() as session:
            deps = tools.Deps(session=session, llm=get_llm(), settings=settings,
                              today=datetime.now(ZoneInfo(settings.timezone)).date())
            payload = fn(deps, **args)
    except tools.InvalidInput as bad:
        payload = tools.error("INVALID_INPUT", bad.message, bad.hint)
    except Exception:  # noqa: BLE001 — logged with the trace; the client gets a plain message
        log.exception("mcp tool failed", extra={"fields": {"tool": name}})
        payload = tools.error("INTERNAL", "The server could not complete this request.",
                              "Try again in a minute.")
    log.info("mcp tool", extra={"fields": {"tool": name, "ok": payload.get("ok")}})
    return _result(payload)


def build(settings: Settings, get_llm: Callable) -> tuple[MCPServer, Starlette]:
    base = settings.public_base_url.strip().rstrip("/")
    resource = base + MCP_PATH
    provider = DbOAuthProvider(base, resource)
    server = MCPServer(
        name="ai-boat", title="AI BOaT farm & expenses", version=tools.VERSION,
        instructions=INSTRUCTIONS, auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base, resource_server_url=resource, validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True)))

    @server.tool(annotations=_READ_ONLY)
    def search_farm_notes(
        question: Annotated[str, Field(description="The question in the user's own words, any "
                                                   "language (romanised Gujarati is common).")],
        question_english: Annotated[str, Field(description="The same question in English. "
                                               "Improves matching. Optional.")] = "",
        plant: Annotated[str, Field(description="Plant or variety name, e.g. mango, Kesar, "
                                    "chiku. Empty = all plants.")] = "",
        date_from: DateArg = "", date_to: DateArg = "",
    ) -> CallToolResult:
        """Search the farm journal and the farmer's advice by meaning and by exact words.
        Returns the matching notes (id, date, text, result), best match first. Use this for
        'what did we do about X', 'what worked for Y', 'what did the farmer say about Z'."""
        return _call(settings, get_llm, "search_farm_notes", tools.search_farm_notes,
                     question=question, question_english=question_english, plant=plant,
                     date_from=date_from, date_to=date_to)

    @server.tool(annotations=_READ_ONLY)
    def last_farm_activity(
        plant: Annotated[str, Field(description="Plant or variety. Empty = all plants.")] = "",
        activity: Annotated[str, Field(description="One of: " + ", ".join(tools.ACTIVITIES)
                                       + ". Empty = any.")] = "",
        limit: Annotated[int, Field(description="How many entries, 1-50.")] = 5,
    ) -> CallToolResult:
        """Newest farm journal entries, newest first. Use for 'when did we last spray the
        mangoes?'. Activity 'planned' means decided but NOT done yet."""
        return _call(settings, get_llm, "last_farm_activity", tools.last_farm_activity,
                     plant=plant, activity=activity, limit=limit)

    @server.tool(annotations=_READ_ONLY)
    def search_expenses(
        text: Annotated[str, Field(description="Words that must all appear in the description "
                                   "or merchant. Empty = any.")] = "",
        category: Annotated[str, Field(description="One of: " + ", ".join(tools.expenses.CATEGORIES)
                                       + ". Empty = any.")] = "",
        paid_by: Annotated[str, Field(description="Co-owner name or nickname. Empty = anyone.")] = "",
        date_from: DateArg = "", date_to: DateArg = "",
        amount_min: Annotated[float | None, Field(description="Rupees, inclusive.")] = None,
        amount_max: Annotated[float | None, Field(description="Rupees, inclusive.")] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-50. The total always covers "
                                    "every match.")] = 50,
    ) -> CallToolResult:
        """Search shared farmhouse expenses, settled and unsettled, newest first. Returns rows
        plus the count and total amount of ALL matches."""
        return _call(settings, get_llm, "search_expenses", tools.search_expenses, text=text,
                     category=category, paid_by=paid_by, date_from=date_from, date_to=date_to,
                     amount_min=amount_min, amount_max=amount_max, limit=limit)

    @server.tool(annotations=_READ_ONLY)
    def get_balance() -> CallToolResult:
        """Current balance of the unsettled expenses: what each co-owner paid, who has an amount
        pending and who gets back, and the suggested settlement transfers."""
        return _call(settings, get_llm, "get_balance", tools.get_balance)

    login_page, login_submit = make_handlers(settings.mcp_owner_password)
    server.custom_route(LOGIN_PATH, methods=["GET"])(login_page)
    server.custom_route(LOGIN_PATH, methods=["POST"])(login_submit)

    host = urlparse(base).netloc
    asgi = server.streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
        # DNS-rebinding guard: only requests addressed to our own host name are served.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host],
            allowed_origins=[base, "https://claude.ai"]))
    return server, asgi
