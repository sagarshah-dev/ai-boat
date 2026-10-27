"""The one place that opens database connections."""
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def normalize_url(url: str) -> str:
    """Neon hands out `postgresql://...`; SQLAlchemy needs to be told to use the psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    return create_engine(
        normalize_url(url),
        # Neon suspends compute when idle; a pooled connection may be dead after that.
        pool_pre_ping=True,
        # Cloud Run runs one instance for this bot, so a small pool is plenty.
        pool_size=3,
        max_overflow=2,
        # Neon's pooled URL goes through PgBouncer in transaction mode, where server-side
        # prepared statements can land on a different backend and fail. Never prepare.
        connect_args={"prepare_threshold": None},
    )


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit if the block finishes, roll back if it raises — so a half-done update never sticks."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
