"""Every route, every caller, one table.

This is the file to read when asking "who can do what". Each row is asserted
against the running app, and `test_every_route_appears_in_the_matrix` fails when
a new route is added without a row - so the table cannot silently fall behind
the API. That completeness check is the reason this exists as a table rather
than as scattered per-route assertions.

Caller legend:
    anonymous  - no credentials at all
    scheduler  - API key with ingest:run, ingest:read
    reader     - API key with insights:read, messages:read
    admin      - API key with the admin scope
    dev-user   - header-based impersonation (non-production only)
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

from tests.conftest import ADMIN_KEY, INGEST_KEY, READER_KEY


# `app.routes` holds `_IncludedRouter` wrappers, not flattened `APIRoute`s -
# `iter_route_contexts` is FastAPI's own way to walk the effective, fully
# prefixed routes.
def _api_route_contexts(app) -> list[RouteContext]:  # type: ignore[no-untyped-def]
    return [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]


# AUTH_MATRIX rows are concrete, executable paths (e.g. `/ingestion/runs/slack`)
# since `test_auth_matrix` fires them at the app verbatim - but FastAPI's
# `route.path` is the template (`/ingestion/runs/{platform}`). Matching one
# against the other needs the template turned into a pattern, not a string
# comparison.
def _template_pattern(route_path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", route_path) + "$")


ANONYMOUS: dict[str, str] = {}
SCHEDULER = {"X-API-Key": INGEST_KEY}
READER = {"X-API-Key": READER_KEY}
ADMIN = {"X-API-Key": ADMIN_KEY}
DEV_USER = {"X-Dev-User": "dev-user"}

OK = 200
#: The ingestion trigger queues a run rather than completing one.
ACCEPTED = 202
UNAUTHENTICATED = 401
FORBIDDEN = 403
#: An authorised caller asking about an id that belongs to nothing. Proves the
#: request got past authentication, which is what these rows are about.
NOT_FOUND = 404

#: (method, path, caller name, headers, expected status)
AUTH_MATRIX: list[tuple[str, str, str, dict[str, str], int]] = [
    # -- unversioned probes: public by design, they carry no data ----------
    ("GET", "/health", "anonymous", ANONYMOUS, OK),
    ("GET", "/api/versions", "anonymous", ANONYMOUS, OK),
    # -- ingestion: machine-to-machine only, one pipeline per platform ------
    ("POST", "/api/v1/ingestion/runs/slack", "anonymous", ANONYMOUS, UNAUTHENTICATED),
    ("POST", "/api/v1/ingestion/runs/slack", "scheduler", SCHEDULER, ACCEPTED),
    ("POST", "/api/v1/ingestion/runs/slack", "reader", READER, FORBIDDEN),
    ("POST", "/api/v1/ingestion/runs/slack", "admin", ADMIN, ACCEPTED),
    ("POST", "/api/v1/ingestion/runs/slack", "dev-user", DEV_USER, FORBIDDEN),
    ("GET", "/api/v1/ingestion/config/slack", "anonymous", ANONYMOUS, UNAUTHENTICATED),
    ("GET", "/api/v1/ingestion/config/slack", "scheduler", SCHEDULER, OK),
    ("GET", "/api/v1/ingestion/config/slack", "reader", READER, FORBIDDEN),
    ("GET", "/api/v1/ingestion/config/slack", "admin", ADMIN, OK),
    # Polling a queued run. The id belongs to nothing, which is fine - these
    # rows assert who gets *past* authentication, not that the run exists.
    ("GET", "/api/v1/ingestion/runs/slack/nope", "anonymous", ANONYMOUS, UNAUTHENTICATED),
    ("GET", "/api/v1/ingestion/runs/slack/nope", "reader", READER, FORBIDDEN),
    # The run history and the live-run indicator. Both carried no scope at all
    # while every sibling route did - the history is the filtering audit trail
    # (every retention verdict, with the message ids it was made about) and the
    # active list reports which pipelines are running. Same scope as the rest of
    # the ingestion surface now.
    ("GET", "/api/v1/ingestion/runs", "anonymous", ANONYMOUS, UNAUTHENTICATED),
    ("GET", "/api/v1/ingestion/runs", "scheduler", SCHEDULER, OK),
    ("GET", "/api/v1/ingestion/runs", "reader", READER, FORBIDDEN),
    ("GET", "/api/v1/ingestion/runs", "admin", ADMIN, OK),
    ("GET", "/api/v1/ingestion/runs/active", "anonymous", ANONYMOUS, UNAUTHENTICATED),
    ("GET", "/api/v1/ingestion/runs/active", "scheduler", SCHEDULER, OK),
    ("GET", "/api/v1/ingestion/runs/active", "reader", READER, FORBIDDEN),
    ("GET", "/api/v1/ingestion/runs/active", "admin", ADMIN, OK),
    # -- insights: personal data. Never anonymous, never the scheduler -----
    ("GET", "/api/v1/insights/users", "anonymous", ANONYMOUS, UNAUTHENTICATED),
    ("GET", "/api/v1/insights/users", "scheduler", SCHEDULER, FORBIDDEN),
    ("GET", "/api/v1/insights/users", "reader", READER, OK),
    ("GET", "/api/v1/insights/users", "admin", ADMIN, OK),
    ("GET", "/api/v1/insights/users", "dev-user", DEV_USER, OK),
    # The per-person summary returns the same PII class as the list above, one
    # row at a time, and used to demand nothing for it. The id belongs to
    # nothing, so an authorised caller gets 404 - these rows assert who gets
    # *past* authentication.
    (
        "GET",
        "/api/v1/insights/users/00000000-0000-4000-8000-000000000000/summary",
        "anonymous",
        ANONYMOUS,
        UNAUTHENTICATED,
    ),
    (
        "GET",
        "/api/v1/insights/users/00000000-0000-4000-8000-000000000000/summary",
        "scheduler",
        SCHEDULER,
        FORBIDDEN,
    ),
    (
        "GET",
        "/api/v1/insights/users/00000000-0000-4000-8000-000000000000/summary",
        "reader",
        READER,
        NOT_FOUND,
    ),
    (
        "GET",
        "/api/v1/insights/users/00000000-0000-4000-8000-000000000000/summary",
        "dev-user",
        DEV_USER,
        NOT_FOUND,
    ),
]

#: Routes intentionally excluded from the matrix, with the reason. Anything not
#: listed here and not in the matrix fails the completeness check.
MATRIX_EXEMPT: dict[str, str] = {
    "get_readiness": (
        "returns 503 in this suite (no database); covered in tests/api/test_system_routes.py"
    ),
}

#: The console surface, deliberately unauthenticated while the two platforms are
#: being wired together.
#:
#: Every one of these calls `require_console_access`, which is one function away
#: from being enforced - see `app/core/security/provisional.py`. Listing them
#: here rather than exempting them keeps the completeness guarantee intact: a new
#: route must still be declared *somewhere*, and the declaration says which of
#: the two states it is in.
#:
#: `test_a_provisionally_open_route_is_actually_open` asserts each of these
#: really is reachable without credentials, so this list cannot quietly start
#: hiding a route that is in fact protected - or, worse, one that was meant to
#: be protected and never was. When roles land, entries move from here into
#: AUTH_MATRIX with real callers and expected statuses.
PROVISIONALLY_OPEN: dict[str, str] = {
    # people and notes
    "list_users": "console directory",
    "create_user": "console directory",
    "get_user": "console directory",
    "update_user": "console directory",
    "forget_user": "console directory - erasure, and the first route to close",
    "list_user_accounts": "console directory",
    "list_user_messages": "console directory",
    "list_user_notes": "console directory",
    "create_user_note": "console directory",
    "delete_note": "console directory",
    # external accounts
    "list_unlinked_accounts": "console integrations",
    "create_account": "console integrations",
    "link_account": "console integrations - reattributes message history",
    "unlink_account": "console integrations",
    "delete_account": "console integrations - destroys messages",
    # messages
    "browse_messages": "console message browser",
    # organization
    "list_org_nodes": "console organization view",
    "create_org_node": "console organization view",
    "update_org_node": "console organization view",
    "delete_org_node": "console organization view",
    "assign_org_member": "console organization view",
    "remove_org_member": "console organization view",
    # ingestion and insights, console-facing halves
    "list_connectors": "console integrations",
    # `list_ingestion_runs`, `get_active_runs` and `get_user_summary` used to
    # live here. They now carry real scope dependencies and have graduated into
    # AUTH_MATRIX above - which is exactly the direction this list is meant to
    # empty in.
}


@pytest.mark.parametrize(
    ("method", "path", "caller", "headers", "expected"),
    [(m, p, c, h, e) for m, p, c, h, e in AUTH_MATRIX],
    ids=[f"{m}-{p}-{c}" for m, p, c, _, _ in AUTH_MATRIX],
)
async def test_auth_matrix(client, method, path, caller, headers, expected) -> None:
    response = await client.request(method, path, headers=headers)
    assert response.status_code == expected, (
        f"{caller} {method} {path} -> {response.status_code}, expected {expected}"
    )


def test_every_route_appears_in_the_matrix(app) -> None:
    """A new endpoint must declare who may call it, or this fails."""
    covered = [(method, path) for method, path, _, _, _ in AUTH_MATRIX]

    missing = []
    for route in _api_route_contexts(app):
        if not route.include_in_schema:
            continue
        if route.name in MATRIX_EXEMPT or route.name in PROVISIONALLY_OPEN:
            continue
        pattern = _template_pattern(route.path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if not any(m == method and pattern.match(p) for m, p in covered):
                missing.append(f"{method} {route.path} ({route.name})")

    assert not missing, (
        "These routes have no entry in AUTH_MATRIX. Add a row for each caller "
        "(or an entry in MATRIX_EXEMPT with a reason):\n  " + "\n  ".join(sorted(missing))
    )


def test_no_matrix_row_points_at_a_route_that_no_longer_exists(app) -> None:
    live = [
        (method, _template_pattern(route.path))
        for route in _api_route_contexts(app)
        for method in route.methods or ()
    ]
    stale = sorted(
        (m, p)
        for m, p, _, _, _ in AUTH_MATRIX
        if not any(m == lm and lp.match(p) for lm, lp in live)
    )
    assert not stale, f"AUTH_MATRIX references routes that no longer exist: {stale}"


def _concrete(path: str) -> str:
    """Fill a templated path with a syntactically valid id.

    The id belongs to nothing, so these requests answer 404 or 422 - which is
    the point. We are asserting that the caller got *past* authentication, not
    that the record exists.
    """
    import re

    return re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)


async def test_a_provisionally_open_route_is_actually_open(app, client) -> None:
    """Every route declared open must really be reachable without credentials.

    Without this, `PROVISIONALLY_OPEN` would be a way to make a route vanish
    from the completeness check by asserting something about it that nobody
    verified.
    """
    unexpectedly_closed = []
    for route in _api_route_contexts(app):
        if route.name not in PROVISIONALLY_OPEN:
            continue
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}:
            response = await client.request(method, _concrete(route.path))
            if response.status_code in (401, 403):
                unexpectedly_closed.append(f"{method} {route.path} -> {response.status_code}")

    assert not unexpectedly_closed, (
        "These routes are declared provisionally open but reject an "
        "unauthenticated caller:\n  " + "\n  ".join(sorted(unexpectedly_closed))
    )


def test_every_provisionally_open_entry_points_at_a_live_route(app) -> None:
    """Stops the list outliving the routes it describes."""
    live = {route.name for route in _api_route_contexts(app)}
    stale = sorted(set(PROVISIONALLY_OPEN) - live)
    assert not stale, f"PROVISIONALLY_OPEN names routes that no longer exist: {stale}"
