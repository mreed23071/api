"""Persisted ingestion history, against a real database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.domains.ingestion.models import IngestionRun, IngestionRunDecision

pytestmark = pytest.mark.integration


async def _run(session, **overrides) -> IngestionRun:
    run = IngestionRun(started_at=datetime.now(UTC), **overrides)
    session.add(run)
    await session.flush()
    return run


async def test_a_run_round_trips_with_its_counters(session) -> None:
    run = await _run(session, fetched=12, retained=9, discarded=3, status="success")

    fetched = await session.get(IngestionRun, run.id)
    assert fetched is not None
    assert (fetched.fetched, fetched.retained, fetched.discarded) == (12, 9, 3)
    assert fetched.dry_run is False


async def test_a_decision_records_a_message_that_was_never_persisted(session) -> None:
    """Deliberately not a foreign key - discarded messages are never stored."""
    run = await _run(session)
    session.add(
        IngestionRunDecision(
            run_id=run.id,
            external_message_id="slack-never-stored",
            keep=False,
            category="personal",
            reason="Personal conversation.",
        )
    )
    await session.flush()

    stored = await session.execute(
        select(IngestionRunDecision).where(IngestionRunDecision.run_id == run.id)
    )
    decision = stored.scalars().one()
    assert decision.keep is False
    assert decision.external_message_id == "slack-never-stored"


async def test_deleting_a_run_removes_its_decisions(session) -> None:
    run = await _run(session)
    session.add(
        IngestionRunDecision(run_id=run.id, external_message_id="slack-1", keep=True)
    )
    await session.flush()

    await session.delete(run)
    await session.flush()

    remaining = await session.execute(select(IngestionRunDecision))
    assert remaining.scalars().all() == []
