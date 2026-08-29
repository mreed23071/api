"""The generated TypeScript SDK is only as good as these guarantees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from app.api.router import API_VERSIONS, default_version
from app.core.config import get_settings
from app.core.openapi import build_version_schema, operation_id_for

REPO_ROOT = Path(__file__).resolve().parents[2]


def _api_route_contexts(app):  # type: ignore[no-untyped-def]
    """`app.routes` holds `_IncludedRouter` wrappers, not flattened `APIRoute`s."""
    return [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]


def published_operations(schema: dict) -> dict[str, dict]:
    return {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


@pytest.fixture
def v1_schema(app) -> dict:  # type: ignore[no-untyped-def]
    settings = get_settings()
    return build_version_schema(
        app, prefix=f"{settings.api_root_prefix}/v1", title="mabinsoft API v1", version="v1"
    )


def test_operation_ids_are_bare_function_names(app) -> None:
    for route in _api_route_contexts(app):
        if route.include_in_schema:
            assert operation_id_for(route) == route.name, (
                f"{route.path} would generate the SDK method "
                f"{operation_id_for(route)!r} instead of {route.name!r}"
            )


def test_operation_ids_are_globally_unique(app) -> None:
    """They are the only thing keeping SDK method names distinct across versions."""
    ids = [operation_id_for(route) for route in _api_route_contexts(app) if route.include_in_schema]
    assert len(ids) == len(set(ids))


def test_v1_publishes_the_expected_operations(v1_schema) -> None:
    """Pins the SDK's method list. Update this set deliberately - a new name
    appearing here means a new method on the generated client.
    """
    assert set(published_operations(v1_schema)) == {
        # ingestion and insights
        "run_ingestion",
        "get_run_status",
        "get_ingestion_config",
        "list_user_summaries",
        "get_user_summary",
        "list_connectors",
        "list_ingestion_runs",
        "get_active_runs",
        # directory: users
        "list_users",
        "get_user",
        "create_user",
        "update_user",
        "forget_user",
        # directory: accounts
        "list_user_accounts",
        "create_account",
        "delete_account",
        "list_unlinked_accounts",
        "link_account",
        "unlink_account",
        # directory: messages and notes
        "list_user_messages",
        "browse_messages",
        "list_user_notes",
        "create_user_note",
        "delete_note",
        # organization
        "list_org_nodes",
        "create_org_node",
        "update_org_node",
        "delete_org_node",
        "assign_org_member",
        "remove_org_member",
    }


def test_every_operation_is_tagged(v1_schema) -> None:
    """Tags drive how the generated client groups methods."""
    for operation_id, operation in published_operations(v1_schema).items():
        assert operation.get("tags"), f"{operation_id} has no tag"


def test_every_operation_documents_the_error_envelope(v1_schema) -> None:
    for operation_id, operation in published_operations(v1_schema).items():
        assert "401" in operation["responses"], f"{operation_id} does not document 401"
        assert "403" in operation["responses"], f"{operation_id} does not document 403"


def test_the_error_envelope_is_in_the_schema(v1_schema) -> None:
    assert "ErrorResponse" in v1_schema["components"]["schemas"]


def test_version_schema_excludes_unversioned_probes(v1_schema) -> None:
    assert "/health" not in v1_schema["paths"]
    assert "/ready" not in v1_schema["paths"]


def test_default_version_is_stable() -> None:
    assert default_version().status == "stable"


def test_committed_schema_matches_the_app(v1_schema) -> None:
    """Guards the frontend: a backend change must be exported and committed.

    CI runs `scripts/export_openapi.py --check`, which is the same assertion
    from the outside; this one gives a fast local signal.
    """
    path = REPO_ROOT / "openapi" / "v1.json"
    if not path.exists():
        pytest.skip("openapi/v1.json not generated yet - run `make openapi`")
    committed = json.loads(path.read_text())
    assert set(published_operations(committed)) == set(published_operations(v1_schema))


def test_every_registered_version_is_mounted(app) -> None:
    settings = get_settings()
    mounted = {route.path for route in _api_route_contexts(app)}
    for version in API_VERSIONS:
        prefix = f"{settings.api_root_prefix}{version.prefix}"
        assert any(path.startswith(prefix) for path in mounted), f"{version.name} is not mounted"
