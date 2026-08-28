"""v1 ingestion routes: auth, wire shape, and the options contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.deps import get_message_source
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from app.domains.ingestion.sources import MockMessageService
from tests.api.conftest import INGEST_HEADERS, READER_HEADERS

RUNS = "/api/v1/ingestion/runs"
CONFIG = "/api/v1/ingestion/config"


@pytest.fixture
def client_with_source(app, client):  # type: ignore[no-untyped-def]
    app.dependency_overrides[get_message_source] = MockMessageService
    return client


async def test_a_run_returns_the_report_shape(client_with_source) -> None:
    response = await client_with_source.post(RUNS, json={}, headers=INGEST_HEADERS)
    assert response.status_code == 200

    body = response.json()
    expected = {
        "run_id",
        "started_at",
        "finished_at",
        "duration_ms",
        "dry_run",
        "fetched",
        "already_ingested",
        "evaluated",
        "retained",
        "discarded",
        "filter_errors",
        "embedded",
        "persisted",
        "users_provisioned",
        "relations_provisioned",
        "filter_provider",
        "filter_prompt_version",
        "embedding_model",
        "decisions",
    }
    assert expected == set(body)
    assert body["fetched"] == 12
    assert body["retained"] + body["discarded"] == body["evaluated"]


async def test_a_run_with_no_body_at_all_is_valid(client_with_source) -> None:
    """The scheduler posts nothing; it must not need to send `{}`."""
    assert (await client_with_source.post(RUNS, headers=INGEST_HEADERS)).status_code == 200


async def test_dry_run_persists_nothing(client_with_source) -> None:
    response = await client_with_source.post(RUNS, json={"dry_run": True}, headers=INGEST_HEADERS)
    body = response.json()
    assert body["dry_run"] is True and body["persisted"] == 0 and body["retained"] > 0


async def test_limit_is_validated_at_the_edge(client_with_source) -> None:
    response = await client_with_source.post(RUNS, json={"limit": 0}, headers=INGEST_HEADERS)
    assert response.status_code == 422


async def test_decisions_expose_the_fallback_flag(client_with_source) -> None:
    body = (await client_with_source.post(RUNS, json={}, headers=INGEST_HEADERS)).json()
    assert body["decisions"]
    assert all("is_fallback" in decision for decision in body["decisions"])


async def test_a_past_runs_decisions_round_trip_through_history(client, seeded_uow) -> None:
    """Regression: a stored run's decisions used to fail to deserialize -
    is_fallback lived only in memory for one run and was never persisted, so
    GET /ingestion/runs 500'd on any run with decisions. The browser reported
    that as a CORS failure, since the error escaped before CORS headers were
    attached to the response."""
    # `default=0`-style column defaults only apply on a real INSERT; this run
    # is never flushed to a session, so every counter needs an explicit value.
    run = IngestionRun(
        id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        duration_ms=0,
        dry_run=False,
        fetched=1,
        already_ingested=0,
        evaluated=1,
        retained=0,
        discarded=1,
        embedded=0,
        persisted=0,
        users_provisioned=0,
        filter_errors=1,
        status="success",
    )
    run.decisions = [
        IngestionRunDecision(
            external_message_id="slack-agent-failed",
            keep=False,
            category="unclear",
            reason="Agent unavailable; fail-closed default applied.",
            is_fallback=True,
        )
    ]
    seeded_uow.runs.runs.append(run)

    response = await client.get(RUNS)

    assert response.status_code == 200
    decision = response.json()[0]["decisions"][0]
    assert decision["is_fallback"] is True


async def test_ingestion_requires_the_ingest_scope(client) -> None:
    assert (await client.post(RUNS, json={})).status_code == 401
    assert (await client.post(RUNS, json={}, headers=READER_HEADERS)).status_code == 403


async def test_config_reports_the_active_pipeline(client) -> None:
    response = await client.get(CONFIG, headers=INGEST_HEADERS)
    assert response.status_code == 200

    body = response.json()
    assert body["llm_provider"] == "stub"
    assert body["embedding_dim"] == 384
    assert body["prompt_version"]
    assert body["filter_system_prompt"]


async def test_config_is_not_public(client) -> None:
    """It returns the retention policy verbatim."""
    assert (await client.get(CONFIG)).status_code == 401


async def test_responses_carry_the_api_version_header(client) -> None:
    response = await client.get(CONFIG, headers=INGEST_HEADERS)
    assert response.headers["X-API-Version"] == "v1"
