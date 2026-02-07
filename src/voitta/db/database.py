"""Database connection and session management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import create_engine, Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from ..config import get_settings
from .models import Base, User


@lru_cache
def get_async_engine() -> AsyncEngine:
    """Get or create the async database engine."""
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@lru_cache
def get_sync_engine() -> Engine:
    """Get or create the sync database engine."""
    settings = get_settings()
    return create_engine(
        settings.sync_database_url,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


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


def init_db() -> None:
    """Initialize database tables and seed default users."""
    from sqlalchemy.orm import Session

    sync_engine = get_sync_engine()

    # Create all tables
    Base.metadata.create_all(bind=sync_engine)

    # Seed default users
    with Session(sync_engine) as session:
        default_users = ["Roman", "Nadya", "Greg"]
        for name in default_users:
            existing = session.query(User).filter(User.name == name).first()
            if not existing:
                session.add(User(name=name))
        session.commit()


def reset_engines() -> None:
    """Reset cached engines - useful for testing."""
    get_async_engine.cache_clear()
    get_sync_engine.cache_clear()
    get_session_factory.cache_clear()
