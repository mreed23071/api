"""v1 wire contracts for messages."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domains.identity.models import Platform
from app.domains.messaging.models import Message


class MessagePreview(BaseModel):
    """Trimmed message embedded in summary responses.

    The embedding is deliberately absent from every v1 read model: 384 floats
    per message is hundreds of kilobytes on a page, and no client can use them.
    Vector access, when it exists, belongs on a search endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform: Platform
    content: str
    sent_at: datetime

    @classmethod
    def from_entity(cls, message: Message) -> "MessagePreview":
        return cls.model_validate(message)


class CommitFile(BaseModel):
    """One file touched by a commit."""

    path: str
    status: str = Field(description='"added", "modified" or "removed".')
    additions: int = 0
    deletions: int = 0


class CommitDetail(BaseModel):
    """The extra structure a commit carries that a chat message does not.

    Stored in the message's `source_metadata` rather than in columns, because it
    is one connector's vocabulary. Putting GitHub's shape into the messages
    table would mean every Slack row carrying empty commit columns forever.
    """

    sha: str
    repository: str
    branch: str | None = None
    url: str | None = None
    files: list[CommitFile] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    ai_summary: str | None = None
    ai_summary_generated_at: datetime | None = None


class MessageRead(BaseModel):
    """A stored message in full, as the browser and person view show it.

    Still no embedding: 384 floats per message is hundreds of kilobytes on a
    page and no client can use them. Vector access belongs on a search endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str = Field(description='"message" or "commit".')
    sender_user_id: uuid.UUID | None = Field(
        default=None, description="Null while the sending account is unattributed."
    )
    sender_relation_id: uuid.UUID | None = None
    platform: Platform
    external_message_id: str
    conversation_id: str | None = None
    content: str
    embedding_model: str | None = None
    filter_category: str | None = None
    filter_reason: str | None = None
    sent_at: datetime
    commit: CommitDetail | None = None

    @classmethod
    def from_entity(cls, message: Message) -> "MessageRead":
        """Build from a row, lifting commit detail out of the metadata blob.

        The blob is connector-supplied, so it is validated rather than trusted:
        a malformed payload yields `commit=None` and a message that still
        renders, instead of a 500 on a screen the user did not ask about.
        """
        read = cls.model_validate(message)
        payload = (message.source_metadata or {}).get("commit")
        if isinstance(payload, dict):
            try:
                read.commit = CommitDetail.model_validate(payload)
            except ValidationError:
                read.commit = None
        return read
