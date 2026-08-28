"""Authentication adapters and the chain that orders them."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.core.errors import AuthenticationError
from app.core.security.principal import PrincipalKind, Scope
from app.core.security.providers import (
    ApiKeyAuthProvider,
    ApiKeyRecord,
    AuthenticationChain,
    DevUserAuthProvider,
)


def make_request(**headers: str) -> Request:
    encoded = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": encoded})


@pytest.fixture
def api_key_provider() -> ApiKeyAuthProvider:
    return ApiKeyAuthProvider(
        [
            ApiKeyRecord(
                secret="s3cret",
                subject="scheduler",
                scopes=frozenset({Scope.INGEST_RUN}),
            )
        ]
    )


async def test_api_key_absent_declines_rather_than_failing(api_key_provider) -> None:
    """No header means 'not my scheme', so the next provider gets a turn."""
    assert await api_key_provider.authenticate(make_request()) is None


async def test_api_key_valid_produces_a_service_principal(api_key_provider) -> None:
    principal = await api_key_provider.authenticate(make_request(**{"X-API-Key": "s3cret"}))
    assert principal is not None
    assert principal.kind is PrincipalKind.SERVICE
    assert principal.subject == "scheduler"
    assert principal.scopes == frozenset({Scope.INGEST_RUN})
    assert principal.auth_scheme == "ApiKey"


async def test_api_key_invalid_raises_instead_of_falling_through(api_key_provider) -> None:
    """Otherwise one request could probe every scheme in the chain."""
    with pytest.raises(AuthenticationError):
        await api_key_provider.authenticate(make_request(**{"X-API-Key": "wrong"}))


async def test_legacy_cron_header_still_works(api_key_provider) -> None:
    principal = await api_key_provider.authenticate(make_request(**{"X-Cron-Token": "s3cret"}))
    assert principal is not None and principal.subject == "scheduler"


async def test_dev_provider_grants_its_default_scopes() -> None:
    provider = DevUserAuthProvider(frozenset({Scope.INSIGHTS_READ}))
    principal = await provider.authenticate(make_request(**{"X-Dev-User": "dev-user"}))
    assert principal is not None
    assert principal.kind is PrincipalKind.USER
    assert principal.scopes == frozenset({Scope.INSIGHTS_READ})


async def test_dev_provider_accepts_a_scope_override() -> None:
    provider = DevUserAuthProvider(frozenset())
    principal = await provider.authenticate(
        make_request(**{"X-Dev-User": "dev-user", "X-Dev-Scopes": "insights:read,messages:read"})
    )
    assert principal is not None
    assert principal.scopes == frozenset({Scope.INSIGHTS_READ, Scope.MESSAGES_READ})


async def test_dev_provider_rejects_an_unknown_scope() -> None:
    provider = DevUserAuthProvider(frozenset())
    with pytest.raises(AuthenticationError):
        await provider.authenticate(
            make_request(**{"X-Dev-User": "m", "X-Dev-Scopes": "insights:write"})
        )


async def test_chain_falls_back_to_anonymous(api_key_provider) -> None:
    chain = AuthenticationChain([api_key_provider])
    principal = await chain.resolve(make_request())
    assert principal.is_authenticated is False


async def test_chain_tries_providers_in_order(api_key_provider) -> None:
    chain = AuthenticationChain(
        [api_key_provider, DevUserAuthProvider(frozenset({Scope.INSIGHTS_READ}))]
    )
    principal = await chain.resolve(
        make_request(**{"X-API-Key": "s3cret", "X-Dev-User": "dev-user"})
    )
    assert principal.auth_scheme == "ApiKey"
    assert chain.schemes == ("ApiKey", "DevUser")
