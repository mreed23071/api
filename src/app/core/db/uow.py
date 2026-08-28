"""Transaction control.

Services decide when work commits. Making that decision explicit - and short -
is what keeps a long-running pipeline from pinning a connection and holding
locks across an LLM call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


class SessionUnitOfWork:
    """Transaction boundaries over one `AsyncSession`.

    Usage:

        async with uow.transaction():
            ...          # committed on clean exit, rolled back on exception

    Nested calls open a SAVEPOINT rather than failing, so a service can compose
    smaller operations that each declare their own boundary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        if self.session.in_transaction():
            async with self.session.begin_nested():
                yield self.session
        else:
            async with self.session.begin():
                yield self.session

    async def flush(self) -> None:
        """Push pending changes so server defaults and ids are populated."""
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
