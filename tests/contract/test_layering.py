"""Architectural rules, enforced instead of documented.

A convention nobody checks is a convention that decays. These are AST checks
over the source tree, so they cost nothing at runtime and fail the moment a
layer starts reaching where it should not.

The dependency direction:

    api      -> domains -> core
    api      -> core
    shared   -> core

and never the other way.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
APP = SRC / "app"


def modules(*relative: str) -> list[Path]:
    paths: list[Path] = []
    for part in relative:
        paths.extend(sorted((APP / part).rglob("*.py")))
    return paths


def imported_modules(path: Path) -> set[str]:
    """Every `app.*` module this file imports, at any level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("app"))
    return found


def rel(path: Path) -> str:
    return str(path.relative_to(SRC))


@pytest.mark.parametrize("path", modules("core"), ids=rel)
def test_core_never_imports_a_bounded_context_or_the_api(path: Path) -> None:
    """`core` is infrastructure. If it needs a domain type, the design is wrong."""
    offenders = {
        name for name in imported_modules(path) if name.startswith(("app.domains", "app.api"))
    }
    assert not offenders, f"{rel(path)} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", modules("domains"), ids=rel)
def test_domains_never_import_the_api_layer(path: Path) -> None:
    """Services must be callable from a worker or a CLI, not just from HTTP."""
    offenders = {name for name in imported_modules(path) if name.startswith("app.api")}
    assert not offenders, f"{rel(path)} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", modules("domains"), ids=rel)
def test_domains_never_import_fastapi(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("fastapi"):
            pytest.fail(f"{rel(path)} imports fastapi; HTTP concerns belong in app/api")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("fastapi"), f"{rel(path)} imports fastapi"


@pytest.mark.parametrize("path", modules("shared"), ids=rel)
def test_shared_never_imports_a_bounded_context_or_the_api(path: Path) -> None:
    offenders = {
        name for name in imported_modules(path) if name.startswith(("app.domains", "app.api"))
    }
    assert not offenders, f"{rel(path)} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", modules("api/v1/routes"), ids=rel)
def test_routes_go_through_services_never_repositories(path: Path) -> None:
    """A route that queries directly bypasses every scope check in the service."""
    offenders = {name for name in imported_modules(path) if name.endswith(".repository")}
    assert not offenders, f"{rel(path)} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", modules("api/v1"), ids=rel)
def test_a_version_never_imports_another_version(path: Path) -> None:
    """Versions must be independently deletable."""
    offenders = {
        name
        for name in imported_modules(path)
        if name.startswith("app.api.v") and not name.startswith("app.api.v1")
    }
    assert not offenders, f"{rel(path)} imports {sorted(offenders)}"


REPOSITORY_SCOPE_EXEMPT = {
    # Writes, not reads: an INSERT carries the tenant on the row itself.
    "bulk_upsert",
}


def _calls(node: ast.AST) -> list[ast.Call]:
    return [inner for inner in ast.walk(node) if isinstance(inner, ast.Call)]


def _calls_scoped(node: ast.AST) -> bool:
    return any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "scoped" for call in _calls(node)
    )


def _self_calls(node: ast.AST) -> set[str]:
    """Names of sibling methods invoked as `self.<name>(...)`."""
    return {
        call.func.attr
        for call in _calls(node)
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    }


@pytest.mark.parametrize(
    "path", [p for p in modules("domains") if p.name == "repository.py"], ids=rel
)
def test_every_reading_repository_method_routes_through_scoped(path: Path) -> None:
    """The tenancy seam only works if nothing bypasses it.

    See `Repository.scoped` - when `organization_id` lands, implementing that one
    method must make every existing query tenant-safe. That is only true if every
    SELECT reaches it, either directly or by delegating to a sibling method that
    does (`count()` builds an aggregate over `_select()`, for instance).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for klass in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
        methods = [
            node for node in klass.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        scoped_methods = {node.name for node in methods if _calls_scoped(node)}

        for node in methods:
            if node.name in REPOSITORY_SCOPE_EXEMPT:
                continue
            builds_select = any(
                isinstance(call.func, ast.Name) and call.func.id == "select"
                for call in _calls(node)
            )
            if not builds_select:
                continue

            satisfied = _calls_scoped(node) or bool(_self_calls(node) & scoped_methods)
            assert satisfied, (
                f"{rel(path)}::{klass.name}.{node.name} builds a SELECT without "
                "reaching self.scoped(), directly or through a sibling method; "
                "it would silently ignore tenant isolation."
            )


@pytest.mark.parametrize("path", modules("workflows"), ids=rel)
def test_workflows_never_import_the_api_layer(path: Path) -> None:
    """The worker is an entrypoint of its own.

    It runs in a container with no HTTP server in it, so reaching into
    `app.api` for anything - even a dependency helper - would mean the worker
    could not start without FastAPI's DI graph. The API's half of the boundary
    lives in `app/api/workflow_gateway.py` and points this way, not back.
    """
    offenders = {name for name in imported_modules(path) if name.startswith("app.api")}
    assert not offenders, f"{rel(path)} imports {sorted(offenders)}"
