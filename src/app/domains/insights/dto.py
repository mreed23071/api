"""Internal contracts published by the insights context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.core.pagination import Paginated
from app.domains.identity.models import User, UserRelation
from app.domains.messaging.models import Message


@dataclass(slots=True)
class UserCommunicationSummary:
    """One user plus the agent's read of their communication history.

    Carries ORM entities rather than wire types: the API version decides what of
    this is public and in what shape.
    """

    user: User
    relations: list[UserRelation] = field(default_factory=list)
    recent_messages: list[Message] = field(default_factory=list)
    message_count: int = 0
    summary: str | None = None
    summary_error: str | None = None
    generated_at: datetime | None = None


@dataclass(slots=True)
class UserSummariesResult:
    """A page of summaries plus the provenance of the generation."""

    page: Paginated[UserCommunicationSummary]
    llm_provider: str
    llm_model: str


@dataclass(frozen=True, slots=True)
class SummaryWindow:
    """The slice of someone's history a summary was asked to cover.

    Both bounds are optional and inclusive. `SummaryWindow()` - both `None` -
    means "everything retained", which is the default the console opens with.

    Deliberately no human-readable label. The mock API returned one ("since
    2026-05-01", "all retained history"), but the console is localised: a
    sentence written in English on the server cannot be translated on the
    client. The dates travel, and the console builds the phrase from its own
    translation tokens.
    """

    starting: datetime | None = None
    ending: datetime | None = None

    @property
    def is_unbounded(self) -> bool:
        """True when no narrowing was asked for at all."""
        return self.starting is None and self.ending is None


@dataclass(slots=True)
class PersonSummary:
    """One person's generated summary for one window of their history.

    `summary` and `summary_error` are mutually exclusive: exactly one of them is
    set. A generation that fails degrades this one person's entry rather than
    the request, so the console can render "we could not summarise this person"
    beside everyone else's summary instead of showing an error page.
    """

    user_id: uuid.UUID
    window: SummaryWindow
    message_count: int
    recent_messages: list[Message] = field(default_factory=list)
    summary: str | None = None
    summary_error: str | None = None
    generated_at: datetime | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
