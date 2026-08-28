"""Operation-id policy, tested without building the whole app."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.core.openapi import (
    assert_unique_operation_ids,
    collect_operation_ids,
    custom_generate_unique_id,
)


def build_app(*names: str) -> FastAPI:
    app = FastAPI(generate_unique_id_function=custom_generate_unique_id)
    for index, name in enumerate(names):
        async def handler() -> dict:  # noqa: ANN202
            return {}

        handler.__name__ = name
        app.add_api_route(f"/path-{index}", handler, name=name)
    return app


def test_operation_id_is_the_bare_function_name() -> None:
    app = build_app("list_user_summaries")
    assert collect_operation_ids(app.routes) == ["list_user_summaries"]


def test_duplicate_names_fail_at_construction_not_at_generation() -> None:
    app = build_app("list_user_summaries", "list_user_summaries")
    with pytest.raises(RuntimeError, match="Duplicate OpenAPI operation ids"):
        assert_unique_operation_ids(app)


def test_distinct_names_pass() -> None:
    assert_unique_operation_ids(build_app("run_ingestion", "list_user_summaries"))
