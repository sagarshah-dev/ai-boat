"""Alembic entry point: runs migrations against DATABASE_URL."""
import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv

from app.db.models import Base
from app.db.session import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # Tests pass the URL in explicitly; otherwise DATABASE_URL from the environment or .env.
    url = config.attributes.get("database_url")
    if not url:
        load_dotenv()
        url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _ours(obj, name, type_, reflected, compare_to) -> bool:
    """LangGraph's checkpoint tables (migration 0005) belong to the library, not to models.py:
    the model-vs-migration check must not report them as tables to drop."""
    return not (type_ == "table" and reflected and str(name).startswith("checkpoint"))


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_server_default=True, include_object=_ours)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) mode is not used in this project")
run_migrations_online()
