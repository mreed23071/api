"""Root test configuration.

Environment defaults are set at import time, before `app.core.config` is
imported by anything, so the whole suite runs against a known configuration
regardless of the developer's shell.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("EMBEDDING_WARMUP_ON_STARTUP", "false")
os.environ.setdefault("DEV_AUTH_ENABLED", "true")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://threadline:threadline@localhost:5432/threadline_test"
)
os.environ.setdefault(
    "API_KEYS",
    (
        '[{"key": "test-ingest-key", "subject": "test-scheduler", '
        '"scopes": ["ingest:run", "ingest:read"]},'
        ' {"key": "test-reader-key", "subject": "test-reader", '
        '"scopes": ["insights:read", "messages:read"]},'
        ' {"key": "test-admin-key", "subject": "test-admin", "scopes": ["admin"]}]'
    ),
)

import pytest  # noqa: E402

from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.core.security.dependencies import reset_auth_cache  # noqa: E402
from app.core.security.principal import (  # noqa: E402
    Principal,
    PrincipalKind,
    Scope,
    TenantContext,
)

#: Credentials that match the API_KEYS above. Tests reference these constants
#: rather than repeating literals.
INGEST_KEY = "test-ingest-key"
READER_KEY = "test-reader-key"
ADMIN_KEY = "test-admin-key"


@pytest.fixture(scope="session", autouse=True)
def _clean_caches() -> None:
    """Settings and the auth chain are process-wide singletons; reset once."""
    reset_settings_cache()
    reset_auth_cache()


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings()


def make_principal(*scopes: Scope, kind: PrincipalKind = PrincipalKind.SERVICE) -> Principal:
    """Build a principal with exactly the scopes a test needs."""
    return Principal(
        subject="test",
        kind=kind,
        scopes=frozenset(scopes),
        tenant=TenantContext.global_scope(),
        auth_scheme="test",
    )


@pytest.fixture
def anonymous_principal() -> Principal:
    return Principal.anonymous()


@pytest.fixture
def ingest_principal() -> Principal:
    return make_principal(Scope.INGEST_RUN, Scope.INGEST_READ)


@pytest.fixture
def reader_principal() -> Principal:
    return make_principal(
        Scope.INSIGHTS_READ, Scope.MESSAGES_READ, kind=PrincipalKind.USER
    )


@pytest.fixture
def admin_principal() -> Principal:
    return make_principal(Scope.ADMIN)


@pytest.fixture
def pipeline_principal() -> Principal:
    """Everything the ingestion pipeline needs end to end."""
    return make_principal(
        Scope.INGEST_RUN, Scope.INGEST_READ, Scope.INSIGHTS_READ, Scope.MESSAGES_READ
    )


# ---------------------------------------------------------------------------
# Application fixtures
#
# Defined at the root so both the api/ and contract/ suites can use them. They
# build the real app - real routers, real dependency graph, real auth chain,
# real schema mapping - with only the unit of work, the LLM and the embedder
# replaced. No Postgres, no torch, no network.
# ---------------------------------------------------------------------------

INGEST_HEADERS = {"X-API-Key": INGEST_KEY}
READER_HEADERS = {"X-API-Key": READER_KEY}
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}


@pytest.fixture
def seeded_uow():  # type: ignore[no-untyped-def]
    """One user with one linked identity and two messages."""
    from tests.factories import make_message, make_relation, make_user
    from tests.fakes import FakeUnitOfWork

    user = make_user(full_name="Alice Nguyen", email="alice@example.com")
    relation = make_relation(user)
    user.__dict__["relations"] = [relation]
    return FakeUnitOfWork(
        users=[user],
        relations=[relation],
        messages=[
            make_message(user, sender_relation_id=relation.id) for _ in range(2)
        ],
    )


@pytest.fixture
def embeddings():  # type: ignore[no-untyped-def]
    from tests.fakes import FakeEmbeddingService

    return FakeEmbeddingService()


@pytest.fixture
def app(seeded_uow, embeddings):  # type: ignore[no-untyped-def]
    from app.api.deps import get_message_source, get_uow
    from app.main import create_app
    from app.shared.embeddings.service import get_embedding_service
    from app.shared.llm.factory import get_llm_client
    from app.shared.llm.stub import StubLLMClient
    from tests.fakes.sources import ScriptedMessageSource

    instance = create_app(get_settings())
    instance.dependency_overrides[get_uow] = lambda: seeded_uow
    instance.dependency_overrides[get_llm_client] = StubLLMClient
    instance.dependency_overrides[get_embedding_service] = lambda: embeddings
    instance.dependency_overrides[get_message_source] = lambda: ScriptedMessageSource([])
    return instance


@pytest.fixture
async def client(app):  # type: ignore[no-untyped-def]
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as async_client:
        yield async_client
