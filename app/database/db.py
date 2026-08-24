"""Database engine, session helpers and schema initialisation.

The rest of the application is asynchronous while SQLAlchemy here is used
synchronously; every call from async code goes through :func:`run_db`, which
hands the work to a worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base

logger = logging.getLogger(__name__)

T = TypeVar("T")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Enable WAL and foreign keys on every new connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def init_engine(database_url: str, echo: bool = False) -> Engine:
    """Create the engine and session factory. Idempotent."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        database_url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    event.listen(_engine, "connect", _configure_sqlite)
    _session_factory = sessionmaker(
        bind=_engine, autoflush=False, expire_on_commit=False, future=True
    )
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("database engine is not initialised; call init_engine() first")
    return _engine


#: Columns added after v1.0.0. ``create_all`` only creates missing *tables*, so
#: an existing database needs these applied by hand. Each entry is
#: ``(table, column, DDL type + default)``; adding a column is idempotent
#: because we check ``PRAGMA table_info`` first.
SCHEMA_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("channels", "resolve_url", "TEXT NOT NULL DEFAULT ''"),
    ("channels", "fallback_urls", "TEXT NOT NULL DEFAULT ''"),
    ("channels", "active_source_index", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "failover_after_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "failback_after_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "auto_failback", "VARCHAR(16) NOT NULL DEFAULT 'inherit'"),
    ("channels", "seamless_switch", "BOOLEAN NOT NULL DEFAULT 0"),
)


def apply_migrations(engine: Engine) -> list[str]:
    """Add columns introduced by a newer version to an existing database.

    Returns the list of columns that were added, so startup can log it.
    """
    applied: list[str] = []
    with engine.begin() as connection:
        for table, column, ddl in SCHEMA_ADDITIONS:
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # table does not exist yet; create_all will make it
            if column in existing:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
            )
            applied.append(f"{table}.{column}")
            logger.info("migration: added column %s.%s", table, column)
    return applied


def init_db(database_url: str, echo: bool = False) -> Engine:
    """Create the engine, the schema, and bring an older database up to date."""
    engine = init_engine(database_url, echo=echo)
    Base.metadata.create_all(engine)
    apply_migrations(engine)
    logger.info("database ready at %s", database_url)
    return engine


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
        logger.info("database engine disposed")
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, roll back on error."""
    if _session_factory is None:
        raise RuntimeError("database is not initialised; call init_db() first")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def call_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``func(session, *args, **kwargs)`` inside a transaction."""
    with session_scope() as session:
        return func(session, *args, **kwargs)


async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Async wrapper around :func:`call_db` using a worker thread."""
    return await asyncio.to_thread(call_db, func, *args, **kwargs)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with session_scope() as session:
        yield session
