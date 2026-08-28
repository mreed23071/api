"""Ingestion context - the persisted history of pipeline runs.

Until now a run report existed only as the response to the request that caused
it: useful for the caller, invisible to everyone else, and gone the moment the
process restarted. Persisting it is what lets the console show a run history,
and what will later let an operator answer "when did the filter start rejecting
everything" without reading logs.

The decisions table is the audit trail for the filtering policy. It is the
evidence behind every retention choice, which is exactly what makes tuning the
filter prompt a reviewable act rather than a guess.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base
from app.core.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domains.identity.models import Platform, platform_enum


class IngestionRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of fetch, filter, embed, store - and what it did.

    Counters are stored rather than derived. They are a record of what the
    pipeline observed at the time, and recomputing them later from the messages
    that survived would silently lose everything that was discarded.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_ingestion_runs_started_at", "started_at"),)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: A dry run performs every step and deliberately saves nothing, so a policy
    #: change can be tested against real traffic without retaining anything.
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Which pipeline this run belongs to. Nullable only for runs recorded
    #: before ingestion was split into one pipeline per platform.
    platform: Mapped[Platform | None] = mapped_column(platform_enum)

    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    already_ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retained: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    users_provisioned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filter_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    filter_provider: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(255))

    #: "success" | "partial" | "failed". A plain string for the same reason
    #: `Message.filter_category` is one: these vocabularies grow, and widening a
    #: native enum costs a migration that a string does not.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")

    decisions: Mapped[list[IngestionRunDecision]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IngestionRun {self.id} {self.status}>"


class IngestionRunDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Why one message was kept or discarded by one run."""

    __tablename__ = "ingestion_run_decisions"
    __table_args__ = (Index("ix_ingestion_run_decisions_run_id", "run_id"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The message as the source platform identified it. Not a foreign key: a
    #: discarded message is never persisted, so half of these would dangle.
    external_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    keep: Mapped[bool] = mapped_column(Boolean, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    #: True when the agent failed and this is the fail-closed default, not a
    #: real judgement - see FilterDecisionDto, where the value originates.
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[IngestionRun] = relationship(back_populates="decisions", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IngestionRunDecision {self.external_message_id} keep={self.keep}>"
