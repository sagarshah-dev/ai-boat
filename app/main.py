"""App entry point: builds the FastAPI app and mounts the routes."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.core.logging import setup_logging
from app.db.session import session_scope
from app.jobs.routes import router as jobs_router
from app.mcp.server import build as build_mcp
from app.telegram.webhook import get_llm, router as telegram_router

log = logging.getLogger(__name__)


class _LateMount:
    """ASGI app the MCP routes are mounted on. The real MCP app is built at every start-up:
    its session manager can run only once, and tests start the same app many times."""

    def __init__(self):
        self.current = None

    async def __call__(self, scope, receive, send):
        if self.current is None:       # a request before start-up finished: not ready yet
            await JSONResponse({"error": "starting"}, status_code=503)(scope, receive, send)
            return
        await self.current(scope, receive, send)


def create_app() -> FastAPI:
    mount = _LateMount() if get_settings().mcp_enabled else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Reading settings here makes a missing env var stop the container at start-up,
        # where Cloud Run shows it as a failed deploy instead of a silent broken bot.
        settings = get_settings()
        setup_logging(settings.log_level)
        log.info("AI BOaT starting", extra={"fields": {"mcp": mount is not None}})
        if mount is None:
            yield
            return
        # Through dependency_overrides, like the bot's routes: tests swap Gemini for a fake once.
        llm = lambda: app.dependency_overrides.get(get_llm, get_llm)()  # noqa: E731
        server, mount.current = build_mcp(settings, llm)
        async with server.session_manager.run():
            yield
        mount.current = None

    app = FastAPI(title="AI BOaT", lifespan=lifespan)
    app.include_router(telegram_router)
    app.include_router(jobs_router)
    app.get("/health")(health)
    if mount is not None:
        # Mounted last: the bot's own routes above always win; the MCP app gets the rest
        # (/mcp, /oauth/login, /authorize, /token, /register, /revoke, /.well-known/*).
        app.mount("/", mount)
    return app


def health() -> JSONResponse:
    """Liveness + database check. Also the cheap call that wakes a cold instance.

    `revision` is set by Cloud Run itself (K_REVISION): one look at /health tells which deploy is
    actually serving — an old revision can keep the traffic when a new one fails to start."""
    revision = os.environ.get("K_REVISION", "local")
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        log.exception("health check: database unreachable")
        return JSONResponse({"status": "error", "db": "unreachable", "revision": revision},
                            status_code=503)
    return JSONResponse({"status": "ok", "db": "ok", "revision": revision})


app = create_app()
