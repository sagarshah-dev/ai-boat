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
from app.telegram.webhook import router as telegram_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Reading settings here makes a missing env var stop the container at start-up,
    # where Cloud Run shows it as a failed deploy instead of a silent broken bot.
    settings = get_settings()
    setup_logging(settings.log_level)
    log.info("AI BOaT starting")
    yield


app = FastAPI(title="AI BOaT", lifespan=lifespan)
app.include_router(telegram_router)
app.include_router(jobs_router)


@app.get("/health")
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
