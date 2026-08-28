"""Identity context - who a person is, and every platform handle they own."""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base
from app.core.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domains.messaging.models import Message


class Platform(StrEnum):
    """Third-party systems we can ingest identities and messages from."""

    SLACK = "slack"
    GITHUB = "github"
    TEAMS = "teams"
    EMAIL = "email"
    LINEAR = "linear"
    OTHER = "other"


#: Shared type object so the `platform` PostgreSQL enum is created exactly once.
platform_enum = Enum(
    Platform,
    name="platform",
    native_enum=True,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person, as this system knows them - independent of any platform."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    job_title: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Personal data with a shorter useful life than the rest of the record.
    #: Kept nullable so a person can exist before anyone has filled it in.
    address: Mapped[str | None] = mapped_column(String(500))
    employment_start: Mapped[date | None] = mapped_column(Date)
    employment_end: Mapped[date | None] = mapped_column(Date)

    #: No `delete-orphan`, deliberately. Unlinking an account detaches it
    #: from the person and keeps the row - it becomes an unlinked account
    #: waiting to be attributed to somebody else. Erasure still removes
    #: these rows, but through the database's ON DELETE CASCADE rather than
    #: through the ORM collection.
    relations: Mapped[list[UserRelation]] = relationship(
        back_populates="user",
        passive_deletes=True,
        lazy="raise",
    )
    notes: Mapped[list[PersonNote]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="sender",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email}>"


class UserRelation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Maps one third-party identity onto one internal `User`.

    This is the join that lets a Slack handle, a GitHub login and a Teams UPN
    all resolve to the same person, so their messages summarise as one history.
    """

    __tablename__ = "user_relations"
    __table_args__ = (
        # An external identity belongs to exactly one internal user.
        UniqueConstraint("platform", "external_id", name="uq_user_relations_platform_external_id"),
        Index("ix_user_relations_user_id_platform", "user_id", "platform"),
    )

    #: Nullable: an account can be known before it is attributed to anybody.
    #: Ingestion discovers external identities, and matching one to a person
    #: is a separate, reversible act - so "unlinked" has to be a state the
    #: schema can express rather than a row that does not exist yet.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    platform: Mapped[Platform] = mapped_column(platform_enum, nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_handle: Mapped[str | None] = mapped_column(String(255))
    external_email: Mapped[str | None] = mapped_column(String(320))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Provider-specific payload we do not want to model as columns yet.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    user: Mapped[User | None] = relationship(back_populates="relations", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserRelation {self.platform}:{self.external_id}>"


class PersonNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A human observation about a person, kept alongside the generated ones.

    Deliberately separate from the summaries in the insights context: those are
    derived and can be regenerated, these were written by somebody and cannot.
    The distinction matters at erasure time, and it matters when a summary and
    a note disagree.
    """

    __tablename__ = "person_notes"
    __table_args__ = (Index("ix_person_notes_user_id_created_at", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Free text rather than a foreign key, until operators are rows themselves.
    #: A note outlives the account of whoever wrote it, so the attribution has
    #: to survive that account being removed.
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    user: Mapped[User] = relationship(back_populates="notes", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PersonNote {self.id} user={self.user_id}>"
