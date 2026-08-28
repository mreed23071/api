"""v1 HTTP surface for browsing stored messages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import MessageServiceDep, PageParamsDep
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.common import Page
from app.api.v1.schemas.messaging import MessageRead
from app.domains.identity.models import Platform
from app.domains.messaging.dto import MessageFilters

router = APIRouter(prefix="/messages", tags=["messaging"], responses=AUTH_RESPONSES)


@router.get(
    "",
    response_model=Page[MessageRead],
    summary="Browse stored messages",
    description=(
        "Every filter is optional; omitting all of them returns the most recent "
        "messages. The date bounds are inclusive, and the search is a "
        "case-insensitive substring match against the message body - wildcard "
        "characters in it are treated literally.\n\n"
        "Offset-paged: `limit` and `offset` bound the query in the database, so "
        "the response size never grows with the size of the table."
    ),
)
async def browse_messages(
    service: MessageServiceDep,
    page: PageParamsDep,
    platform: Annotated[Platform | None, Query(description="Restrict to one platform.")] = None,
    category: Annotated[
        str | None, Query(description="Filtering verdict, e.g. 'business'.")
    ] = None,
    sent_from: Annotated[
        datetime | None, Query(description="Inclusive lower bound on when it was sent.")
    ] = None,
    sent_to: Annotated[
        datetime | None, Query(description="Inclusive upper bound on when it was sent.")
    ] = None,
    search: Annotated[
        str | None, Query(description="Case-insensitive substring of the body.")
    ] = None,
) -> Page[MessageRead]:
    filters = MessageFilters(
        platform=platform,
        category=category,
        sent_from=sent_from,
        sent_to=sent_to,
        search=search,
    )
    result = await service.browse(filters, page)
    return Page.build(result, MessageRead.from_entity)
