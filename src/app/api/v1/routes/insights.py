"""v1 HTTP surface for Domain 2 - retrieval & summarization."""

from __future__ import annotations

from typing import Annotated

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import InsightsServiceDep, PageParamsDep
from app.domains.insights.dto import SummaryWindow
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.insights import PersonSummaryResponse, UserSummariesResponse
from app.core.security.dependencies import require_scopes
from app.core.security.principal import Scope

router = APIRouter(prefix="/insights", tags=["insights"], responses=AUTH_RESPONSES)


@router.get(
    "/users",
    response_model=UserSummariesResponse,
    dependencies=[Depends(require_scopes(Scope.INSIGHTS_READ, Scope.MESSAGES_READ))],
    summary="List users with agent-generated communication summaries",
    description=(
        "Returns a page of users together with a concise, agent-generated summary "
        "of their communication history.\n\n"
        "This response contains personal data: names, email addresses, linked "
        "third-party accounts, verbatim message excerpts and a behavioural "
        "summary. It requires both `insights:read` and `messages:read`.\n\n"
        "Summaries are generated per request and are not cached, so latency and "
        "cost scale with page size."
    ),
)
async def list_user_summaries(
    service: InsightsServiceDep,
    page: PageParamsDep,
    active_only: Annotated[bool, Query(description="Exclude deactivated users.")] = True,
    messages_per_user: Annotated[
        int, Query(ge=1, le=200, description="Messages considered per user.")
    ] = 25,
) -> UserSummariesResponse:
    result = await service.list_with_summaries(
        page, active_only=active_only, messages_per_user=messages_per_user
    )
    return UserSummariesResponse.from_result(result)


@router.get(
    "/users/{user_id}/summary",
    response_model=PersonSummaryResponse,
    summary="Summarise one person's communication history",
    description=(
        "The single-person counterpart to the list endpoint. Both bounds are "
        "optional and inclusive; omitting them summarises everything retained."
        "\n\n"
        "Nothing is cached: every call reaches the model. That is free and "
        "deterministic under the stub provider and would not be with a real one, "
        "so persisting summaries against an input fingerprint is deliberate "
        "later work rather than an omission.\n\n"
        "A window containing no messages is not an error - it returns a "
        "`summary_error` explaining there was nothing to describe."
    ),
)
async def get_user_summary(
    service: InsightsServiceDep,
    user_id: Annotated[uuid.UUID, Path(description="The person to summarise.")],
    range_from: Annotated[
        datetime | None, Query(description="Inclusive start of the window.")
    ] = None,
    range_to: Annotated[
        datetime | None, Query(description="Inclusive end of the window.")
    ] = None,
    recent: Annotated[
        int, Query(ge=1, le=50, description="How many recent messages to include.")
    ] = 5,
) -> PersonSummaryResponse:
    summary = await service.summarize_person(
        user_id, SummaryWindow(starting=range_from, ending=range_to), recent=recent
    )
    return PersonSummaryResponse.from_dto(summary)
