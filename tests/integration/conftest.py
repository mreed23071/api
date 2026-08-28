"""Integration fixtures: a real PostgreSQL with pgvector, per test session.

These are the tests that verify the things a fake cannot: the migration itself,
the `ON CONFLICT` idempotency guarantee, the window function, and that asyncpg
can round-trip a `vector` column. They need Docker; `make test` skips them and
`make test-integration` runs them.

Isolation is per test: each test runs inside a transaction that is rolled back
afterwards, so the schema is migrated exactly once and no test can see another's
rows.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.security.principal import TenantContext

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Applies to every module in this package - one marker, one skip switch.
pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip(
    "testcontainers.postgres",
    reason="testcontainers is required for integration tests: uv sync --group dev",
)

#: Must carry the vector extension; the stock postgres image does not.
POSTGRES_IMAGE = os.environ.get("TEST_POSTGRES_IMAGE", "pgvector/pgvector:pg17")


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Start a throwaway pgvector instance for the whole session."""
    with testcontainers.PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def migrated(database_url: str) -> str:
    """Apply the real migration chain, exactly as the container entrypoint does.

    Running Alembic rather than `metadata.create_all` is the point: it is the
    migration that ships, so it is the migration that must be tested.
    """
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
    return database_url


@pytest.fixture
async def engine(migrated: str):  # type: ignore[no-untyped-def]
    instance = create_async_engine(migrated, poolclass=None)
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """A session whose writes are always rolled back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async_session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await transaction.rollback()


@pytest.fixture
def uow(session: AsyncSession):  # type: ignore[no-untyped-def]
    from app.domains.uow import UnitOfWork

    return UnitOfWork(session, TenantContext.global_scope())
