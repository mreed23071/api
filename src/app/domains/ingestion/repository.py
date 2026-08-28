"""Data access for the ingestion context - the persisted history of runs.

Read-mostly. One write per pipeline execution, and a list query behind the
console's run history screen.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db.repository import Repository
from app.domains.identity.models import Platform
from app.domains.ingestion.models import IngestionRun


class IngestionRunRepository(Repository):
    """Reads and writes rows in `ingestion_runs` and `ingestion_run_decisions`."""

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
