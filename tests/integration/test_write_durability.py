"""Does a write actually survive the request, or only look like it did?

Every other integration test in this package runs inside `conftest.py`'s
`session` fixture, which wraps the whole test in one outer transaction and
rolls it back at teardown for isolation. That makes it structurally unable to
answer the question this file asks: a service that does a SAVEPOINT instead
of a real commit looks identical to one that commits for real, right up until
the process restarts or a second connection looks for the row - both are
already inside the fixture's outer transaction, so a read sees the write
either way.

This file deliberately does not use that fixture. It builds the real app with
real dependencies (no `FakeUnitOfWork`, no overridden `get_uow`), sends it a
real HTTP request, and then checks for the row through a *second, independent*
connection - the one thing a request's own session cannot fake. Each test
cleans up whatever it inserted, since nothing here is wrapped in a rollback.

This is the shape of test that would have caught the SAVEPOINT-vs-commit bug
in `SessionUnitOfWork` (a read before `uow.transaction()` autobegins a
transaction, so the write takes the nested-SAVEPOINT branch and nothing ever
commits the outer one) - every other integration test's isolation mechanism
hid it by construction.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings, reset_settings_cache
from app.core.db.engine import reset_engine_cache
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def live_client(migrated: str, monkeypatch) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """The real app, real `get_uow`, pointed at the real test database.

    Unlike every other API test's `client` fixture, nothing here is faked or
    overridden - a request through this client takes exactly the path a
    production request takes, transaction boundaries included.
    """
    monkeypatch.setenv("DATABASE_URL", migrated)
    reset_settings_cache()
    reset_engine_cache()
    app = create_app(Settings(database_url=migrated, app_env="test"))
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        reset_engine_cache()
        reset_settings_cache()


@pytest.fixture
async def outside_connection(migrated: str) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """A connection the request never touched - the only honest way to ask
    "is this row actually in the database" rather than "does this session
    still remember writing it"."""
    engine = create_async_engine(migrated, poolclass=None)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def test_creating_a_person_survives_the_request(
    live_client: AsyncClient, outside_connection: AsyncSession
) -> None:
    """The exact shape of the bug report: POST succeeds, GET afterwards
    (through a fresh connection, not the one that wrote it) must find it."""
    email = f"durability-{uuid.uuid4().hex[:8]}@example.com"
    try:
        response = await live_client.post(
            "/api/v1/users",
            json={"full_name": "Durability Check", "email": email, "job_title": "QA"},
        )
        assert response.status_code == 201

        row = await outside_connection.execute(
            text("SELECT email FROM users WHERE email = :email"), {"email": email}
        )
        assert row.scalar_one_or_none() == email, (
            "the API reported success but the row is not visible from a "
            "second connection - the write never actually committed"
        )
    finally:
        await outside_connection.execute(
            text("DELETE FROM users WHERE email = :email"), {"email": email}
        )
        await outside_connection.commit()


async def test_creating_an_org_node_survives_the_request(
    live_client: AsyncClient, outside_connection: AsyncSession
) -> None:
    """Same bug, same fix, different service - `create_node` also reads
    (validates the parent, when one is given) before writing. This call has
    no parent, which is deliberately the *harder* case to catch: with no
    preceding read, this one path alone wouldn't have surfaced the bug."""
    name = f"durability-{uuid.uuid4().hex[:8]}"
    try:
        response = await live_client.post("/api/v1/org/nodes", json={"name": name, "subtitle": ""})
        assert response.status_code == 201

        row = await outside_connection.execute(
            text("SELECT name FROM org_nodes WHERE name = :name"), {"name": name}
        )
        assert row.scalar_one_or_none() == name
    finally:
        await outside_connection.execute(
            text("DELETE FROM org_nodes WHERE name = :name"), {"name": name}
        )
        await outside_connection.commit()


async def test_a_rejected_write_does_not_commit(
    live_client: AsyncClient, outside_connection: AsyncSession
) -> None:
    """The other half of the fix: `get_session` now commits unconditionally
    on a clean exit, so a validation failure must still roll back rather than
    partially writing whatever it built before raising."""
    response = await live_client.post(
        "/api/v1/users",
        json={"full_name": "", "email": "should-not-exist@example.com", "job_title": ""},
    )
    assert response.status_code in (400, 422)

    row = await outside_connection.execute(
        text("SELECT 1 FROM users WHERE email = :email"), {"email": "should-not-exist@example.com"}
    )
    assert row.scalar_one_or_none() is None
