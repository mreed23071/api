"""Engine, sessionmaker and the request-scoped session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_seconds,
        # Explicit, and shorter than SQLAlchemy's 30s default. Pool exhaustion
        # otherwise surfaces as an opaque half-minute hang - including on
        # `/health` - with nothing to distinguish it from a slow database.
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )
    return engine


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    A service still opens its own transaction boundary via
    `SessionUnitOfWork.transaction()` - that choice is what lets a service
    compose several short, explicit transactions (see the ingestion pipeline)
    instead of pinning one connection for the whole request. But the session
    autobegins a transaction on its *first* statement, including a plain read
    - so a method that checks something exists before writing it (almost every
    write in this codebase) already has a transaction open by the time it
    calls `uow.transaction()`. `SessionUnitOfWork.transaction()` sees that and
    opens a SAVEPOINT instead of a real transaction, correctly - but then
    nothing ever commits the *outer* transaction the SAVEPOINT lived inside,
    and closing an uncommitted session rolls it back. The write looks like it
    succeeded (`flush()` still assigns a real id) and then silently vanishes.
    Committing here, once, on a clean exit, closes out whatever transaction
    the session ended up in - real or SAVEPOINT-nested - the same way a
    request that never explicitly opened one still needs its rollback
    guaranteed below.

    Long-running work (LLM calls, external I/O) must not run while this session
    holds a transaction. Services performing fan-out must call
    `UnitOfWork.checkpoint()` (a commit that releases the pooled connection)
    after their last read and before the slow phase.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()


def reset_engine_cache() -> None:
    """Drop the cached engine/sessionmaker. Test-support only."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: For code paths that must own their own short-lived sessions rather than
#: borrow the request-scoped one - anything that interleaves database work with
#: slow external calls. `get_sessionmaker` is `lru_cache`-decorated and sync,
#: which `Depends` accepts directly; wrapping it in an async shim would only add
#: a thread hop.
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
