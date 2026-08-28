"""OpenAPI policy: operation ids and per-version schema export.

FastAPI's default unique id is `"{name}_{path}_{method}"`, which @hey-api turns
into `listUserSummariesApiV1InsightsUsersGet()`. We override it so the operation
id is nothing but the endpoint function name, and the SDK reads
`listUserSummaries()`.

The trade-off, stated plainly: the function name becomes the only thing keeping
operation ids unique, *across every version*. Two options exist; this codebase
takes the first:

1. **Globally unique endpoint function names** (chosen). A v2 endpoint that
   changes shape is named `list_user_summaries_v2`; one that does not change is
   simply re-exported into the v2 router from v1. `assert_unique_operation_ids`
   makes a collision a startup failure rather than a route silently vanishing
   from the generated client.
2. Prefix the operation id with the version (`v1_list_user_summaries`), giving
   `v1ListUserSummaries()` in TypeScript. Unambiguous, uglier at every call site.

Because clients pin a version, the export script also writes one schema file per
version (`openapi/v1.json`), and each generated SDK contains only its version's
operations.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from starlette.routing import BaseRoute


def custom_generate_unique_id(route: APIRoute) -> str:
    """Use the endpoint's function name verbatim as the operation id."""
    return route.name


def operation_id_for(route: APIRoute | RouteContext) -> str:
    """What the OpenAPI document will actually publish for this route."""
    return route.operation_id or route.unique_id


def collect_operation_ids(routes: list[BaseRoute]) -> list[str]:
    # `app.routes` holds `_IncludedRouter` wrappers, not flattened `APIRoute`s -
    # `iter_route_contexts` is FastAPI's own way to walk the effective, fully
    # prefixed routes (the same one `get_openapi` uses internally).
    return [
        operation_id_for(context)
        for context in iter_route_contexts(routes)
        if isinstance(context.original_route, APIRoute) and context.include_in_schema
    ]


def assert_unique_operation_ids(app: FastAPI) -> None:
    """Raise if two routes would generate the same SDK method name."""
    duplicates = sorted(
        name for name, count in Counter(collect_operation_ids(app.routes)).items() if count > 1
    )
    if duplicates:
        raise RuntimeError(
            "Duplicate OpenAPI operation ids would produce clashing SDK methods: "
            f"{', '.join(duplicates)}. Endpoint function names must be globally "
            "unique across API versions - see app/core/openapi.py."
        )


def build_version_schema(app: FastAPI, *, prefix: str, title: str, version: str) -> dict[str, Any]:
    """Produce an OpenAPI document containing only one version's routes.

    This is what the frontend generates its client from, so a client pinned to
    v1 cannot accidentally call a v2 operation.
    """
    routes = [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and (context.path or "").startswith(prefix)
    ]
    if not routes:
        raise ValueError(f"No routes are mounted under {prefix!r}")
    return get_openapi(
        title=title,
        version=version,
        description=app.description,
        routes=routes,
    )
