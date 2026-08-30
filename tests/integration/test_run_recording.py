"""Recording a run is idempotent, against a real Postgres.

`record_run`'s docstring has always claimed the write is "transactional and
idempotent". Transactional it was; idempotent it was not. The activity did a
plain ORM insert with no key to conflict on, so the exact failure Temporal is
built to survive - a worker that crashes after its transaction commits but
before it acks the activity - produced two `ingestion_runs` rows and two full
decision sets for one run when the activity was retried.

These tests exercise the real `ON CONFLICT` against the real constraint, because
that is the part a fake cannot verify: the constraint's name, its column, and
whether the upsert actually resolves in the database rather than in Python.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.domains.identity.models import Platform
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from app.domains.ingestion.repository import RUN_ID_CONSTRAINT

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def run_values(run_id: uuid.UUID, **overrides):  # type: ignore[no-untyped-def]
    values = {
        "run_id": run_id,
        "started_at": datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 29, 12, 5, tzinfo=UTC),
        "duration_ms": 300_000,
        "dry_run": False,
        "platform": Platform.SLACK,
        "fetched": 10,
        "already_ingested": 0,
        "evaluated": 10,
        "retained": 8,
        "discarded": 2,
        "embedded": 8,
        "persisted": 8,
        "users_provisioned": 1,
        "filter_errors": 0,
        "filter_provider": "stub",
        "embedding_model": "nomic-embed-text",
        "status": "success",
    }
    values.update(overrides)
    return values


def decision_rows(count: int = 3):  # type: ignore[no-untyped-def]
    return [
        {
            "external_message_id": f"m{index}",
            "keep": True,
            "category": "business",
            "reason": None,
            "is_fallback": False,
        }
        for index in range(count)
    ]


async def count_runs(session, run_id: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    statement = select(func.count()).select_from(IngestionRun).where(IngestionRun.run_id == run_id)
    return (await session.execute(statement)).scalar_one()


async def count_decisions(session, run_pk: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    statement = (
        select(func.count())
        .select_from(IngestionRunDecision)
        .where(IngestionRunDecision.run_id == run_pk)
    )
    return (await session.execute(statement)).scalar_one()


async def test_recording_the_same_run_twice_leaves_one_row(uow, session) -> None:  # type: ignore[no-untyped-def]
    """Acceptance check H3.

    This is the worker-crash-then-retry scenario, reproduced exactly: the same
    summary recorded twice.
    """
    run_id = uuid.uuid4()

    first_pk = await uow.runs.upsert_by_run_id(run_values(run_id))
    await uow.runs.replace_decisions(first_pk, decision_rows())

    second_pk = await uow.runs.upsert_by_run_id(run_values(run_id))
    await uow.runs.replace_decisions(second_pk, decision_rows())

    assert first_pk == second_pk, "the retry created a second row instead of updating the first"
    assert await count_runs(session, run_id) == 1
    assert await count_decisions(session, first_pk) == 3


async def test_the_upsert_finalises_the_api_s_queued_row_in_place(uow, session) -> None:  # type: ignore[no-untyped-def]
    """The normal path: the API inserts `queued`, the workflow finalises it.

    Both writes address the same row through `run_id`, which is the whole reason
    the column exists - the API and the worker previously had no shared key and
    so could only ever produce two unrelated rows.
    """
    run_id = uuid.uuid4()

    queued_pk = await uow.runs.upsert_by_run_id(
        {
            "run_id": run_id,
            "platform": Platform.SLACK,
            "dry_run": False,
            "started_at": datetime(2026, 8, 29, 11, 59, tzinfo=UTC),
            "status": "queued",
        }
    )
    final_pk = await uow.runs.upsert_by_run_id(run_values(run_id))

    assert queued_pk == final_pk
    assert await count_runs(session, run_id) == 1

    stored = await uow.runs.get_by_run_id(run_id)
    assert stored is not None
    assert stored.status == "success"
    assert stored.persisted == 8
    # The queued row's placeholder timestamp is replaced by the workflow's own.
    assert stored.started_at == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


async def test_a_run_started_without_a_queued_row_inserts_cleanly(uow, session) -> None:  # type: ignore[no-untyped-def]
    """A workflow started directly - a test, a legacy caller - still records.

    Both paths converge on one row; that they converge is what lets `record_run`
    stop caring which one it is on.
    """
    run_id = uuid.uuid4()

    pk = await uow.runs.upsert_by_run_id(run_values(run_id))

    assert await count_runs(session, run_id) == 1
    stored = await uow.runs.get_by_run_id(run_id)
    assert stored is not None and stored.id == pk


async def test_replacing_decisions_removes_the_previous_set(uow, session) -> None:  # type: ignore[no-untyped-def]
    """Delete-then-insert, not append.

    A retry whose verdicts differ (a re-run against a changed prompt) must leave
    the run describing one coherent set, not the union of two.
    """
    run_id = uuid.uuid4()
    pk = await uow.runs.upsert_by_run_id(run_values(run_id))

    await uow.runs.replace_decisions(pk, decision_rows(5))
    assert await count_decisions(session, pk) == 5

    await uow.runs.replace_decisions(pk, decision_rows(2))
    assert await count_decisions(session, pk) == 2


async def test_two_runs_may_judge_the_same_message(uow, session) -> None:  # type: ignore[no-untyped-def]
    """`uq_run_decision_message` is scoped to the run, not global.

    A message re-evaluated by a later run is normal and must not collide - the
    constraint exists to stop one run recording a verdict twice.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    first_pk = await uow.runs.upsert_by_run_id(run_values(first))
    second_pk = await uow.runs.upsert_by_run_id(run_values(second))

    await uow.runs.replace_decisions(first_pk, decision_rows(2))
    await uow.runs.replace_decisions(second_pk, decision_rows(2))

    assert await count_decisions(session, first_pk) == 2
    assert await count_decisions(session, second_pk) == 2


async def test_the_run_id_constraint_is_named_what_the_upsert_says_it_is(session) -> None:  # type: ignore[no-untyped-def]
    """The upsert names this constraint in SQL, so a rename in a future
    migration would break the write at runtime with nothing catching it first.
    Same discipline as `IDEMPOTENCY_CONSTRAINT` in the messaging repository.
    """
    from sqlalchemy import text

    found = (
        await session.execute(
            text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name "
                "AND conrelid = 'ingestion_runs'::regclass"
            ),
            {"name": RUN_ID_CONSTRAINT},
        )
    ).scalar_one_or_none()

    assert found == 1, f"{RUN_ID_CONSTRAINT} is missing from ingestion_runs"


async def test_legacy_rows_without_a_run_id_do_not_collide(uow, session) -> None:  # type: ignore[no-untyped-def]
    """`run_id` is nullable for rows recorded before the column existed.

    Postgres treats NULLs as distinct under a unique constraint, so any number
    of legacy rows coexist - which is what let the column be added without
    inventing identifiers for history.
    """
    for _ in range(3):
        session.add(
            IngestionRun(
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                duration_ms=0,
                platform=Platform.SLACK,
                status="success",
            )
        )
    await session.flush()

    total = (
        await session.execute(
            select(func.count()).select_from(IngestionRun).where(IngestionRun.run_id.is_(None))
        )
    ).scalar_one()
    assert total >= 3
