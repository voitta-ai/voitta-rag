"""Database connection and session management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import create_engine, event, Engine, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from ..config import get_settings
from .models import Base, User


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite pragmas on every new connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


@lru_cache
def get_async_engine() -> AsyncEngine:
    """Get or create the async database engine."""
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool,
    )
    event.listens_for(engine.sync_engine, "connect")(_set_sqlite_pragmas)
    return engine


@lru_cache
def get_sync_engine() -> Engine:
    """Get or create the sync database engine."""
    settings = get_settings()
    engine = create_engine(
        settings.sync_database_url,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool,
    )
    event.listens_for(engine, "connect")(_set_sqlite_pragmas)
    return engine


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the session factory."""
    return async_sessionmaker(
        get_async_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides a database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database session (use outside of FastAPI dependencies)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _migrate_missing_columns(engine: Engine) -> None:
    """Add any columns defined in models but missing from the SQLite database."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                col_type = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}")
                    )


def init_db() -> None:
    """Initialize database tables and seed default users."""
    from sqlalchemy.orm import Session

    sync_engine = get_sync_engine()

    # Create all tables
    Base.metadata.create_all(bind=sync_engine)

    # Add any new columns to existing tables
    _migrate_missing_columns(sync_engine)

    # Seed users from users.txt if enabled
    import os
    from pathlib import Path

    seed_enabled = os.getenv("VOITTA_SEED_USERS", "false").lower() == "true"
    if seed_enabled:
        users_file = Path(os.getenv("VOITTA_USERS_FILE", "users.txt"))
        if users_file.exists():
            names = [
                line.strip()
                for line in users_file.read_text().splitlines()
                if line.strip()
            ]
            with Session(sync_engine) as session:
                for name in names:
                    existing = session.query(User).filter(User.name == name).first()
                    if not existing:
                        session.add(User(name=name))
                session.commit()


def reset_engines() -> None:
    """Reset cached engines - useful for testing."""
    get_async_engine.cache_clear()
    get_sync_engine.cache_clear()
    get_session_factory.cache_clear()
