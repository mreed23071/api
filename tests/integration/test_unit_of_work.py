"""Transaction boundaries. These are what stop a long pipeline pinning a
connection and holding locks across a network call."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.domains.identity.models import User
from tests.factories import make_user

pytestmark = pytest.mark.integration


async def count_users(session) -> int:  # type: ignore[no-untyped-def]
    return int((await session.execute(select(func.count()).select_from(User))).scalar_one())


async def test_a_clean_transaction_commits(uow) -> None:
    async with uow.transaction():
        await uow.users.add(make_user())
    assert await count_users(uow.session) == 1


async def test_an_exception_rolls_the_whole_block_back(uow) -> None:
    with pytest.raises(RuntimeError):
        async with uow.transaction():
            await uow.users.add(make_user())
            raise RuntimeError("boom")
    assert await count_users(uow.session) == 0


async def test_nested_transactions_use_a_savepoint(uow) -> None:
    """A service must be able to compose operations that declare their own boundary."""
    async with uow.transaction():
        await uow.users.add(make_user())
        async with uow.transaction():
            await uow.users.add(make_user())
    assert await count_users(uow.session) == 2


async def test_an_inner_failure_does_not_have_to_lose_the_outer_work(uow) -> None:
    async with uow.transaction():
        await uow.users.add(make_user())
        with pytest.raises(RuntimeError):
            async with uow.transaction():
                await uow.users.add(make_user())
                raise RuntimeError("inner")
    assert await count_users(uow.session) == 1


async def test_repositories_share_the_unit_of_work_session(uow) -> None:
    user = await uow.users.add(make_user())
    assert await uow.users.get(user.id) is not None
