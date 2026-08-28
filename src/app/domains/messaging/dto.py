"""Internal contracts published by the messaging context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domains.identity.models import Platform


class NewMessage(BaseModel):
    """A message ready to be stored: filtered, embedded, attributed."""

    model_config = ConfigDict(frozen=True)

    platform: Platform
    external_message_id: str = Field(max_length=255)
    conversation_id: str | None = Field(default=None, max_length=255)
    content: str
    sent_at: datetime
    kind: str = "message"
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    sender_user_id: uuid.UUID
    sender_relation_id: uuid.UUID | None = None

    embedding: list[float] | None = None
    embedding_model: str | None = None

    filter_category: str | None = None
    filter_reason: str | None = None
    filter_prompt_version: str | None = None

    @property
    def key(self) -> tuple[Platform, str]:
        """The pair that identifies a message uniquely: platform plus its id there.

        Matches the `(platform, external_message_id)` unique constraint, which is
        what makes re-running ingestion safe - a message already stored is
        recognised rather than duplicated.
        """
        return (self.platform, self.external_message_id)


@dataclass(frozen=True, slots=True)
class MessageFilters:
    """What the message browser is asking for.

    Every field is optional, and `None` means "do not narrow by this". So an
    empty `MessageFilters()` is a request for everything - which is exactly what
    the console sends before the user touches any of the controls.

    `frozen=True` makes instances read-only after construction, so a filter set
    cannot be quietly modified halfway down the call stack. `slots=True` is a
    memory optimisation that also has a useful side effect: assigning to a field
    name that does not exist raises instead of silently creating one, which
    turns a typo into an error.
    """

    #: Narrow to one person. The summariser sets this; the browser does not.
    user_id: uuid.UUID | None = None
    platform: Platform | None = None
    category: str | None = None
    #: Inclusive lower bound on `sent_at`.
    sent_from: datetime | None = None
    #: Inclusive upper bound on `sent_at`.
    sent_to: datetime | None = None
    #: Case-insensitive substring match against the message body.
    search: str | None = None
