"""Structural completeness, including the test-coverage map.

`SOURCE_COVERAGE` below is the answer to "what is tested, and by what" at a
glance. Every module under `src/app` must appear as a key, mapped either to the
test modules that exercise it or to `Untested(reason)`.

Two failures are possible, and both are useful:

* a new source module was added and nobody decided how it is tested;
* a test module named in the map no longer exists.

Line coverage (`make cov`) tells you how much of a module ran. This tells you
whether anyone *intended* to test it, which is the question that actually goes
stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
APP = SRC / "app"
TESTS = REPO_ROOT / "tests"


@dataclass(frozen=True)
class Untested:
    """A module deliberately left without a dedicated test, and why."""

    reason: str


UNIT = "tests/unit"
API = "tests/api"
CONTRACT = "tests/contract"
INTEGRATION = "tests/integration"

SOURCE_COVERAGE: dict[str, list[str] | Untested] = {
    # -- api layer ---------------------------------------------------------
    "app/api/deps.py": [f"{API}/v1/test_ingestion_routes.py", f"{API}/v1/test_insights_routes.py"],
    "app/api/errors.py": [f"{API}/test_errors.py"],
    "app/api/v1/routes/accounts.py": [f"{API}/v1/test_directory_routes.py"],
    "app/api/v1/routes/connectors.py": [f"{API}/v1/test_organization_routes.py"],
    "app/api/v1/routes/messages.py": [f"{API}/v1/test_directory_routes.py"],
    "app/api/v1/routes/organization.py": [f"{API}/v1/test_organization_routes.py"],
    "app/api/v1/routes/people.py": [f"{API}/v1/test_directory_routes.py"],
    "app/api/v1/schemas/directory.py": [f"{API}/v1/test_directory_routes.py"],
    "app/api/v1/schemas/organization.py": [f"{API}/v1/test_organization_routes.py"],
    "app/api/router.py": [f"{API}/test_system_routes.py", f"{CONTRACT}/test_openapi_contract.py"],
    "app/api/system.py": [f"{API}/test_system_routes.py"],
    "app/api/v1/routes/ingestion.py": [f"{API}/v1/test_ingestion_routes.py"],
    "app/api/v1/routes/insights.py": [f"{API}/v1/test_insights_routes.py"],
    "app/api/v1/schemas/common.py": [f"{API}/v1/test_insights_routes.py"],
    "app/api/v1/schemas/identity.py": [f"{API}/v1/test_insights_routes.py"],
    "app/api/v1/schemas/ingestion.py": [f"{API}/v1/test_ingestion_routes.py"],
    "app/api/v1/schemas/insights.py": [f"{API}/v1/test_insights_routes.py"],
    "app/api/v1/schemas/messaging.py": [f"{API}/v1/test_insights_routes.py"],
    # -- core --------------------------------------------------------------
    "app/core/config.py": [f"{UNIT}/core/test_config.py"],
    "app/core/errors.py": [f"{API}/test_errors.py"],
    "app/core/logging.py": [f"{UNIT}/core/test_logging.py"],
    "app/core/middleware.py": [f"{API}/test_system_routes.py"],
    "app/core/openapi.py": [f"{UNIT}/core/test_openapi.py", f"{CONTRACT}/test_openapi_contract.py"],
    "app/core/pagination.py": [f"{UNIT}/core/test_pagination.py"],
    "app/core/db/base.py": Untested("Declarative base and naming convention; no behaviour."),
    "app/core/db/mixins.py": Untested("Column declarations; exercised by every integration test."),
    "app/core/db/engine.py": [
        f"{INTEGRATION}/test_migrations.py",
        f"{INTEGRATION}/test_write_durability.py",
    ],
    "app/core/db/repository.py": [
        f"{CONTRACT}/test_layering.py",
        f"{INTEGRATION}/domains/test_identity_repository.py",
    ],
    "app/core/db/uow.py": [f"{INTEGRATION}/test_unit_of_work.py"],
    "app/core/security/principal.py": [f"{UNIT}/core/security/test_principal.py"],
    "app/core/security/providers.py": [f"{UNIT}/core/security/test_providers.py"],
    "app/core/security/dependencies.py": [f"{CONTRACT}/test_auth_matrix.py"],
    "app/core/security/provisional.py": [f"{UNIT}/core/security/test_provisional.py"],
    # -- domains -----------------------------------------------------------
    "app/domains/uow.py": [f"{INTEGRATION}/test_unit_of_work.py"],
    "app/domains/identity/directory.py": [f"{UNIT}/domains/identity/test_directory.py"],
    "app/domains/identity/dto.py": [
        f"{UNIT}/domains/identity/test_service.py",
        f"{UNIT}/domains/identity/test_directory.py",
    ],
    "app/domains/identity/models.py": [f"{INTEGRATION}/domains/test_identity_repository.py"],
    "app/domains/identity/repository.py": [f"{INTEGRATION}/domains/test_identity_repository.py"],
    "app/domains/identity/service.py": [f"{UNIT}/domains/identity/test_service.py"],
    "app/domains/ingestion/dto.py": [
        f"{UNIT}/domains/ingestion/test_service.py",
        f"{UNIT}/domains/ingestion/test_connectors.py",
    ],
    "app/domains/ingestion/models.py": [f"{INTEGRATION}/domains/test_ingestion_run_models.py"],
    "app/domains/ingestion/repository.py": [f"{INTEGRATION}/domains/test_ingestion_run_models.py"],
    "app/domains/ingestion/filtering.py": [f"{UNIT}/domains/ingestion/test_filtering.py"],
    "app/domains/ingestion/service.py": [
        f"{UNIT}/domains/ingestion/test_service.py",
        f"{UNIT}/domains/ingestion/test_connectors.py",
        f"{INTEGRATION}/test_ingestion_pipeline.py",
    ],
    "app/domains/ingestion/sources.py": [f"{UNIT}/domains/ingestion/test_sources.py"],
    "app/domains/insights/dto.py": [
        f"{UNIT}/domains/insights/test_service.py",
        f"{UNIT}/domains/insights/test_person_summary.py",
    ],
    "app/domains/insights/service.py": [
        f"{UNIT}/domains/insights/test_service.py",
        f"{UNIT}/domains/insights/test_person_summary.py",
    ],
    "app/domains/insights/summarization.py": [f"{UNIT}/domains/insights/test_summarization.py"],
    "app/domains/messaging/dto.py": [
        f"{UNIT}/domains/ingestion/test_service.py",
        f"{UNIT}/domains/messaging/test_browse.py",
    ],
    "app/domains/messaging/models.py": [f"{INTEGRATION}/domains/test_message_repository.py"],
    "app/domains/messaging/repository.py": [f"{INTEGRATION}/domains/test_message_repository.py"],
    "app/domains/messaging/service.py": [
        f"{UNIT}/domains/ingestion/test_service.py",
        f"{UNIT}/domains/messaging/test_browse.py",
    ],
    "app/domains/organization/dto.py": [f"{UNIT}/domains/organization/test_service.py"],
    "app/domains/organization/models.py": [f"{INTEGRATION}/domains/test_organization_models.py"],
    "app/domains/organization/repository.py": [
        f"{INTEGRATION}/domains/test_organization_models.py"
    ],
    "app/domains/organization/service.py": [f"{UNIT}/domains/organization/test_service.py"],
    "app/domains/organization/tree.py": [f"{UNIT}/domains/organization/test_tree.py"],
    # -- shared ------------------------------------------------------------
    "app/shared/embeddings/base.py": Untested("Protocol definition; no behaviour."),
    "app/shared/embeddings/local.py": [f"{UNIT}/shared/embeddings/test_local.py"],
    "app/shared/embeddings/ollama.py": [f"{UNIT}/shared/embeddings/test_ollama.py"],
    "app/shared/embeddings/factory.py": [f"{UNIT}/shared/embeddings/test_factory.py"],
    "app/shared/embeddings/worker.py": Untested(
        "Loads the real sentence-transformer. Covered only by running the "
        "container; see docs/TESTING.md 'What is deliberately untested'."
    ),
    "app/shared/llm/base.py": Untested("Protocol and dataclass definitions; no behaviour."),
    "app/shared/llm/stub.py": [f"{UNIT}/shared/llm/test_stub.py"],
    "app/shared/llm/factory.py": [f"{UNIT}/shared/llm/test_factory.py"],
    "app/shared/llm/ollama_client.py": [f"{UNIT}/shared/llm/test_ollama_client.py"],
    "app/shared/llm/anthropic_client.py": Untested(
        "Thin adapter over a paid network API. Exercising it would test the "
        "vendor SDK, not us; the port contract is covered by the stub tests."
    ),
    # -- workflows ---------------------------------------------------------
    "app/workflows/config.py": [f"{UNIT}/workflows/test_config.py"],
    "app/workflows/dto.py": Untested("Pydantic payload definitions; no behaviour."),
    "app/workflows/client.py": Untested(
        "Opens a gRPC connection to Temporal. The workflow tests use "
        "WorkflowEnvironment, which supplies its own client."
    ),
    "app/workflows/activities.py": [f"{UNIT}/workflows/test_activities.py"],
    "app/workflows/ingestion.py": [f"{UNIT}/workflows/test_ingestion_workflow.py"],
    "app/workflows/runner.py": Untested(
        "Process entrypoint: builds a Worker and blocks. Covered by running "
        "the container; the pieces it wires are tested individually."
    ),
    "app/workflows/gateway.py": [f"{UNIT}/workflows/test_gateway.py"],
    # -- entrypoints -------------------------------------------------------
    "app/main.py": [f"{API}/test_system_routes.py"],
    "app/seed/loader.py": [
        f"{UNIT}/test_seed_loader.py",
        f"{INTEGRATION}/test_seed.py",
    ],
    "app/models.py": [f"{CONTRACT}/test_structure.py"],
}


def source_modules() -> list[str]:
    return sorted(
        str(path.relative_to(SRC)) for path in APP.rglob("*.py") if path.name != "__init__.py"
    )


def test_every_source_module_has_a_coverage_decision() -> None:
    undeclared = [module for module in source_modules() if module not in SOURCE_COVERAGE]
    assert not undeclared, (
        "These modules are not in SOURCE_COVERAGE. Add the test module(s) that "
        "exercise them, or Untested('reason'):\n  " + "\n  ".join(undeclared)
    )


def test_the_map_has_no_stale_entries() -> None:
    live = set(source_modules())
    stale = sorted(module for module in SOURCE_COVERAGE if module not in live)
    assert not stale, f"SOURCE_COVERAGE names modules that no longer exist: {stale}"


@pytest.mark.parametrize(
    ("module", "targets"),
    [(m, t) for m, t in SOURCE_COVERAGE.items() if isinstance(t, list)],
)
def test_named_test_modules_exist(module: str, targets: list[str]) -> None:
    missing = [target for target in targets if not (REPO_ROOT / target).exists()]
    assert not missing, f"{module} points at test modules that do not exist: {missing}"


def test_untested_modules_stay_a_short_list() -> None:
    """A ratchet: if this grows, someone is opting out of testing by default.

    Raised from 6 to 9 when the Temporal worker landed. The three additions are
    the same category as the entries already here - a process entrypoint that
    blocks forever (`workflows/runner.py`), a module that opens a gRPC
    connection (`workflows/client.py`), and payload definitions with no
    behaviour (`workflows/dto.py`). Raise this only for that category, and say
    why here.
    """
    untested = {m for m, t in SOURCE_COVERAGE.items() if isinstance(t, Untested)}
    assert len(untested) <= 9, f"Too many untested modules: {sorted(untested)}"


# ---------------------------------------------------------------------------
# Bounded-context shape
# ---------------------------------------------------------------------------

CONTEXTS = ["identity", "messaging", "ingestion", "insights", "organization"]


@pytest.mark.parametrize("context", CONTEXTS)
def test_every_context_declares_itself(context: str) -> None:
    init = APP / "domains" / context / "__init__.py"
    assert init.exists(), f"{context} has no __init__.py"
    assert init.read_text(encoding="utf-8").strip(), (
        f"{context}/__init__.py is empty; it should state what the context owns "
        "and what it publishes."
    )


@pytest.mark.parametrize("context", CONTEXTS)
def test_every_context_has_a_service(context: str) -> None:
    assert (APP / "domains" / context / "service.py").exists()


def test_every_models_module_is_registered_for_migrations() -> None:
    """Alembic autogenerate only sees tables whose module has been imported."""
    registered = (APP / "models.py").read_text(encoding="utf-8")
    for path in (APP / "domains").rglob("models.py"):
        dotted = "app." + str(path.relative_to(APP).with_suffix("")).replace("/", ".").replace(
            "\\", "."
        )
        assert dotted in registered, (
            f"{dotted} is not imported by app/models.py, so Alembic cannot see its tables."
        )


def test_the_embedding_dimension_matches_the_migration() -> None:
    """Two sources of truth exist; this keeps them honest.

    pgvector fixes the width on the column so the HNSW index can be built, so
    EMBEDDING_DIM and the migration chain must agree exactly - a mismatch is
    not caught until an INSERT fails at runtime. Checked against the migration
    that last set the width, which moves whenever the embedding model changes.
    """
    from app.core.config import Settings

    migration = (REPO_ROOT / "migrations/versions/0005_embedding_dim_768.py").read_text()
    assert f"NEW_DIM = {Settings().embedding_dim}" in migration
