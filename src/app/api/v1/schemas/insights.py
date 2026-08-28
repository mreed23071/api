"""v1 wire contracts for the insights domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.api.v1.schemas.common import Page
from app.api.v1.schemas.identity import UserRead, UserRelationRead
from app.api.v1.schemas.messaging import MessagePreview
from app.domains.insights.dto import (
    PersonSummary,
    UserCommunicationSummary,
    UserSummariesResult,
)


class UserSummaryRead(BaseModel):
    """One user plus the agent's read of their communication history."""

    user: UserRead
    relations: list[UserRelationRead] = Field(
        default_factory=list, description="Every third-party identity linked to this user."
    )
    message_count: int = Field(description="Messages considered when summarising.")
    summary: str | None = Field(
        default=None, description="Agent-generated summary; null when generation failed."
    )
    summary_error: str | None = Field(
        default=None, description="Why the summary is missing, when it is."
    )
    generated_at: datetime | None = None
    recent_messages: list[MessagePreview] = Field(default_factory=list)

    @classmethod
    def from_dto(cls, entry: UserCommunicationSummary) -> "UserSummaryRead":
        return cls(
            user=UserRead.from_entity(entry.user),
            relations=[UserRelationRead.from_entity(r) for r in entry.relations],
            message_count=entry.message_count,
            summary=entry.summary,
            summary_error=entry.summary_error,
            generated_at=entry.generated_at,
            recent_messages=[MessagePreview.from_entity(m) for m in entry.recent_messages],
        )


class UserSummariesResponse(BaseModel):
    """A page of summaries, with the provenance of the generation."""

    page: Page[UserSummaryRead]
    llm_provider: str = Field(description="Which adapter produced these summaries.")
    llm_model: str

    @classmethod
    def from_result(cls, result: UserSummariesResult) -> "UserSummariesResponse":
        return cls(
            page=Page.build(result.page, UserSummaryRead.from_dto),
            llm_provider=result.llm_provider,
            llm_model=result.llm_model,
        )


class PersonSummaryResponse(BaseModel):
    """One person's generated summary for one window of their history.

    No human-readable label for the window. The mock returned one ("since
    2026-05-01"), but the console is localised and a sentence composed on the
    server cannot be translated on the client - so the dates travel and the
    console builds the phrase from its own tokens.

    `summary` and `summary_error` are mutually exclusive: a generation that
    fails degrades this one entry rather than the request.
    """

    user_id: uuid.UUID
    summary: str | None = None
    summary_error: str | None = None
    generated_at: datetime | None = None
    message_count: int = 0
    recent_messages: list[MessagePreview] = Field(default_factory=list)
    range_from: datetime | None = None
    range_to: datetime | None = None
    llm_provider: str | None = None
    llm_model: str | None = None

    @classmethod
    def from_dto(cls, summary: PersonSummary) -> "PersonSummaryResponse":
        return cls(
            user_id=summary.user_id,
            summary=summary.summary,
            summary_error=summary.summary_error,
            generated_at=summary.generated_at,
            message_count=summary.message_count,
            recent_messages=[
                MessagePreview.from_entity(m) for m in summary.recent_messages
            ],
            range_from=summary.window.starting,
            range_to=summary.window.ending,
            llm_provider=summary.llm_provider,
            llm_model=summary.llm_model,
        )
