"""v1 ingestion routes: auth, wire shape, and the per-platform contract.

`TEMPORAL_ENABLED=false` in the fast suite (see tests/conftest.py), so the
trigger runs the pipeline inline and reports it already completed. That keeps
these tests about the *HTTP contract* - status codes, auth, the queued
envelope - while the orchestration they stand in for is covered against a real
in-process Temporal in tests/unit/workflows/.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from tests.api.conftest import INGEST_HEADERS, READER_HEADERS


def runs_url(platform: str) -> str:
    return f"/api/v1/ingestion/runs/{platform}"


def config_url(platform: str) -> str:
    return f"/api/v1/ingestion/config/{platform}"


RUNS = "/api/v1/ingestion/runs"  # the unfiltered history list, not a trigger


async def test_a_trigger_accepts_rather_than_completes(client) -> None:
    """202, not 200. A real run takes minutes against a local model, so the
    caller gets an id to poll rather than a connection held open."""
    response = await client.post(runs_url("slack"), json={}, headers=INGEST_HEADERS)
    assert response.status_code == 202

    body = response.json()
    assert set(body) == {"run_id", "platform", "status", "workflow_id", "dry_run"}
    assert body["platform"] == "slack"
    assert body["run_id"]


async def test_a_run_with_no_body_at_all_is_valid(client) -> None:
    """The scheduler posts nothing; it must not need to send `{}`."""
    response = await client.post(runs_url("slack"), headers=INGEST_HEADERS)
    assert response.status_code == 202


async def test_dry_run_is_carried_through_to_the_queued_run(client) -> None:
    response = await client.post(
        runs_url("slack"), json={"dry_run": True}, headers=INGEST_HEADERS
    )
    assert response.status_code == 202
    assert response.json()["dry_run"] is True


async def test_limit_is_validated_at_the_edge(client) -> None:
    response = await client.post(
        runs_url("slack"), json={"limit": 0}, headers=INGEST_HEADERS
    )
    assert response.status_code == 422


async def test_an_unregistered_platform_is_a_clean_404_not_a_crash(client) -> None:
    """No connector is wired up for email/linear/other yet. The connector is
    resolved *before* queueing, so this is a 404 here rather than a workflow
    that starts and immediately dies."""
    response = await client.post(runs_url("email"), json={}, headers=INGEST_HEADERS)
    assert response.status_code == 404


async def test_an_invalid_platform_is_a_422(client) -> None:
    response = await client.post(runs_url("not-a-real-platform"), json={}, headers=INGEST_HEADERS)
    assert response.status_code == 422


async def test_a_github_run_ingests_commits_not_chat_messages(client) -> None:
    """The whole point of a distinct GitHub connector: commits are a different
    shape, not a chat message with a different label."""
    response = await client.post(runs_url("github"), json={}, headers=INGEST_HEADERS)
    assert response.status_code == 202
    assert response.json()["platform"] == "github"

    stored = await client.get("/api/v1/messages", params={"platform": "github"})
    commits = [m for m in stored.json()["items"] if m["kind"] == "commit"]
    assert commits, "at least one retained GitHub message should be stored as a commit"
    assert commits[0]["commit"]["sha"]
    assert commits[0]["commit"]["repository"]


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
        platform=None,
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


async def test_run_history_can_be_scoped_to_one_platform(client) -> None:
    await client.post(runs_url("slack"), json={}, headers=INGEST_HEADERS)
    await client.post(runs_url("github"), json={}, headers=INGEST_HEADERS)

    response = await client.get(RUNS, params={"platform": "github"})

    assert response.status_code == 200
    body = response.json()
    assert body
    assert all(run["platform"] == "github" for run in body)


async def test_polling_an_unknown_run_is_a_404(client) -> None:
    """With Temporal disabled there is nothing to poll, and the route says so
    rather than pretending the run is queued."""
    response = await client.get(f"{runs_url('slack')}/nope", headers=INGEST_HEADERS)
    assert response.status_code == 404


async def test_ingestion_requires_the_ingest_scope(client) -> None:
    assert (await client.post(runs_url("slack"), json={})).status_code == 401
    assert (
        await client.post(runs_url("slack"), json={}, headers=READER_HEADERS)
    ).status_code == 403


async def test_config_reports_the_active_pipeline(client) -> None:
    response = await client.get(config_url("slack"), headers=INGEST_HEADERS)
    assert response.status_code == 200

    body = response.json()
    assert body["platform"] == "slack"
    assert body["llm_provider"] == "stub"
    assert body["embedding_dim"] == get_settings().embedding_dim
    assert body["prompt_version"]
    assert body["filter_system_prompt"]


async def test_config_is_not_public(client) -> None:
    """It returns the retention policy verbatim."""
    assert (await client.get(config_url("slack"))).status_code == 401


async def test_responses_carry_the_api_version_header(client) -> None:
    response = await client.get(config_url("slack"), headers=INGEST_HEADERS)
    assert response.headers["X-API-Version"] == "v1"
