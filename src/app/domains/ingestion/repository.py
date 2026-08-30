"""Data access for the ingestion context - the persisted history of runs.

Read-mostly. One write per pipeline execution, and a list query behind the
console's run history screen.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from app.core.db.repository import Repository
from app.domains.identity.models import Platform
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision

#: Name of the constraint that makes run recording idempotent. Referenced by the
#: upsert below and asserted by an integration test, so a rename cannot silently
#: turn the recording activity back into a duplicate-writer. Same discipline as
#: `IDEMPOTENCY_CONSTRAINT` in the messaging repository.
RUN_ID_CONSTRAINT = "uq_ingestion_runs_run_id"


class IngestionRunRepository(Repository):
    """Reads and writes rows in `ingestion_runs` and `ingestion_run_decisions`."""

    async def upsert_by_run_id(self, values: Mapping[str, Any]) -> uuid.UUID:
        """Insert or update one run, keyed on the workflow identifier.

        Core rather than ORM on purpose: this has to be a single statement that
        resolves the conflict in the database, not a read-then-write that races
        another attempt of the same activity.

        On the first attempt the API's `"queued"` row is already there, so this
        takes the conflict path and updates it in place. If it is missing - a
        legacy caller, or a workflow started directly in a test - the insert
        succeeds instead. Both converge on one row per `run_id`, which is what
        makes a worker crash between commit and Temporal ack cost nothing.

        Returns the surviving row's surrogate `id`.
        """
        statement = pg_insert(IngestionRun).values(**values)
        # Every column carried in the payload except the conflict key itself, so
        # a re-record refreshes the counters and status rather than leaving the
        # queued row's zeroes in place. `updated_at` is set explicitly: the
        # column's `onupdate` is a SQLAlchemy-side hook and does not fire for the
        # raw `DO UPDATE SET` this compiles to.
        assignments: dict[str, Any] = {
            key: statement.excluded[key] for key in values if key != "run_id"
        }
        assignments["updated_at"] = func.now()
        upsert = statement.on_conflict_do_update(
            constraint=RUN_ID_CONSTRAINT, set_=assignments
        ).returning(IngestionRun.id)
        run_pk: uuid.UUID = (await self.session.execute(upsert)).scalar_one()
        return run_pk

    async def replace_decisions(
        self, run_pk: uuid.UUID, decisions: Sequence[Mapping[str, Any]]
    ) -> int:
        """Make this run's verdicts exactly `decisions`, atomically.

        Delete-then-insert inside the caller's transaction is the idempotency
        mechanism: a retried recording rewrites the same children instead of
        appending a second copy of them. `uq_run_decision_message` is the
        backstop, not the mechanism.
        """
        await self.session.execute(
            delete(IngestionRunDecision).where(IngestionRunDecision.run_id == run_pk)
        )
        if not decisions:
            return 0
        await self.session.execute(
            pg_insert(IngestionRunDecision).values(
                [{**decision, "run_id": run_pk} for decision in decisions]
            )
        )
        return len(decisions)

    async def get_by_run_id(self, run_id: uuid.UUID) -> IngestionRun | None:
        """Fetch one run by its workflow identifier, with its decisions.

        The database-side half of run status: it answers for runs Temporal has
        forgotten (namespace retention) or never saw (a queue that failed to
        start).
        """
        statement = (
            self.scoped(select(IngestionRun), IngestionRun)
            .options(selectinload(IngestionRun.decisions))
            .where(IngestionRun.run_id == run_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def add(self, run: IngestionRun) -> IngestionRun:
        """Store one run and, through the relationship, its decisions.

        Assigning to `run.decisions` before calling this is enough - SQLAlchemy
        inserts the parent, reads back its generated id, and fills that id into
        each child row. There is no need to save the run first and then loop.
        """
        self.session.add(run)
        await self.session.flush()
        return run

    async def list_recent(
        self, *, limit: int = 20, platform: Platform | None = None
    ) -> Sequence[IngestionRun]:
        """The most recent runs, newest first, each with its decisions loaded.

        `selectinload` fetches the decisions in a second query keyed by run id,
        rather than joining. A join would repeat every run's columns once per
        decision - with a few hundred decisions per run, that is a lot of
        duplicated data over the wire to assemble the same objects.

        It is also required rather than optional here: the models declare
        `lazy="raise"`, so reading `run.decisions` without having asked for them
        raises instead of quietly issuing another query per run.
        """
        statement = (
            self.scoped(select(IngestionRun), IngestionRun)
            .options(selectinload(IngestionRun.decisions))
            .order_by(IngestionRun.started_at.desc())
            .limit(limit)
        )
        if platform is not None:
            statement = statement.where(IngestionRun.platform == platform)
        return (await self.session.execute(statement)).scalars().all()

    async def get(self, run_id: uuid.UUID) -> IngestionRun | None:
        """Fetch one run with its decisions, or `None`."""
        statement = (
            self.scoped(select(IngestionRun), IngestionRun)
            .options(selectinload(IngestionRun.decisions))
            .where(IngestionRun.id == run_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()
