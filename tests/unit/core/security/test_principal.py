"""Scope semantics. These rules decide who can read other people's messages."""

from __future__ import annotations

import pytest

from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security.principal import Principal, PrincipalKind, Scope, TenantContext
from tests.conftest import make_principal


def test_anonymous_is_not_authenticated() -> None:
    principal = Principal.anonymous()
    assert principal.is_authenticated is False
    assert principal.kind is PrincipalKind.ANONYMOUS
    assert principal.scopes == frozenset()


def test_anonymous_require_raises_401_not_403() -> None:
    """Never tell an unauthenticated caller that a scope exists."""
    with pytest.raises(AuthenticationError):
        Principal.anonymous().require(Scope.INSIGHTS_READ)


def test_authenticated_without_scope_raises_403() -> None:
    principal = make_principal(Scope.INGEST_RUN)
    with pytest.raises(AuthorizationError) as excinfo:
        principal.require(Scope.INSIGHTS_READ)
    assert excinfo.value.details["missing_scopes"] == ["insights:read"]


def test_require_accepts_multiple_scopes() -> None:
    principal = make_principal(Scope.INSIGHTS_READ, Scope.MESSAGES_READ)
    principal.require(Scope.INSIGHTS_READ, Scope.MESSAGES_READ)


def test_require_reports_every_missing_scope_at_once() -> None:
    principal = make_principal(Scope.INSIGHTS_READ)
    with pytest.raises(AuthorizationError) as excinfo:
        principal.require(Scope.INSIGHTS_READ, Scope.MESSAGES_READ, Scope.INGEST_RUN)
    assert excinfo.value.details["missing_scopes"] == ["messages:read", "ingest:run"]


def test_admin_satisfies_every_scope() -> None:
    admin = make_principal(Scope.ADMIN)
    for scope in Scope:
        assert admin.has(scope)


def test_principal_is_immutable() -> None:
    principal = make_principal(Scope.ADMIN)
    with pytest.raises(Exception):
        principal.subject = "someone-else"  # type: ignore[misc]


def test_tenant_defaults_to_global() -> None:
    assert TenantContext.global_scope().is_global is True
    assert TenantContext(tenant_id="acme").is_global is False


def test_scope_parse_rejects_unknown_values() -> None:
    assert Scope.parse(" insights:read ") is Scope.INSIGHTS_READ
    with pytest.raises(ValueError, match="unknown scope"):
        Scope.parse("insights:write")


def test_audit_fields_identify_the_caller() -> None:
    fields = make_principal(Scope.ADMIN).audit_fields()
    assert fields["principal_subject"] == "test"
    assert fields["principal_kind"] == "service"
