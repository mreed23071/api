"""Composition root.

Everything a route needs is built here from request scope, so routes stay
declarative and every dependency has exactly one override point in tests
(`app.dependency_overrides[get_message_source] = ...`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Query

from app.core.config import Settings, get_settings
from app.core.db.engine import SessionDep
from app.core.errors import NotFoundError
from app.core.pagination import MAX_LIMIT, PageParams
from app.core.security.dependencies import CurrentPrincipal
from app.domains.identity.directory import DirectoryService
from app.domains.identity.models import Platform
from app.domains.ingestion.dto import RawMessage
from app.domains.ingestion.service import IngestionService
from app.domains.ingestion.sources import MessageSource, MockChatSource, MockGitHubCommitSource
from app.domains.insights.service import UserInsightsService
from app.domains.messaging.service import MessageService
from app.domains.organization.service import OrganizationService
from app.domains.uow import UnitOfWork
from app.shared.embeddings.service import EmbeddingService, get_embedding_service
from app.shared.llm.base import LLMClient
from app.shared.llm.factory import get_llm_client

SettingsDep = Annotated[Settings, Depends(get_settings)]
LLMDep = Annotated[LLMClient, Depends(get_llm_client)]
EmbeddingDep = Annotated[EmbeddingService, Depends(get_embedding_service)]


def get_uow(session: SessionDep, principal: CurrentPrincipal) -> UnitOfWork:
    """One unit of work per request, bound to the caller's tenant.

    Because the tenant comes from the principal rather than a parameter, no
    route can accidentally construct a repository that spans tenants.
    """
    return UnitOfWork(session, principal.tenant)


UowDep = Annotated[UnitOfWork, Depends(get_uow)]


# -- console-surface services ---------------------------------------------
#
# Each is a one-line factory rather than being constructed in the route, so a
# route declares what it needs and a test overrides one entry point rather than
# patching a constructor. The access decision lives inside each service, not
# here - see `app.core.security.provisional`.


def get_directory_service(uow: UowDep, principal: CurrentPrincipal) -> DirectoryService:
    """People, their accounts and the notes kept about them."""
    return DirectoryService(uow, principal)


DirectoryServiceDep = Annotated[DirectoryService, Depends(get_directory_service)]


def get_organization_service(uow: UowDep, principal: CurrentPrincipal) -> OrganizationService:
    """The department hierarchy and its membership."""
    return OrganizationService(uow, principal)


OrganizationServiceDep = Annotated[OrganizationService, Depends(get_organization_service)]


def get_message_service(uow: UowDep, principal: CurrentPrincipal) -> MessageService:
    """Stored messages, for browsing."""
    return MessageService(uow, principal)


MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]


def get_page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Items per page.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Items to skip.")] = 0,
) -> PageParams:
    """Shared pagination query parameters.

    Bounds are declared here once, so every paginated endpoint in the version
    documents and enforces the same limits.
    """
    return PageParams(limit=limit, offset=offset)


PageParamsDep = Annotated[PageParams, Depends(get_page_params)]


#: One pipeline per platform, not one pipeline for all of them - each entry is
#: swapped for a real connector independently, on its own schedule, without
#: touching the others. `Callable[[], MessageSource]`, not an instance: a fresh
#: connector per request, same as every other per-request dependency here.
_MOCK_SOURCES: dict[Platform, Callable[[], MessageSource]] = {
    Platform.GITHUB: MockGitHubCommitSource,
    Platform.SLACK: lambda: MockChatSource(Platform.SLACK, name="slack-mock"),
    Platform.TEAMS: lambda: MockChatSource(Platform.TEAMS, name="teams-mock"),
}


class _NoConnector:
    """Stands in for routes that build an `IngestionService` without ever
    calling `.run()` on it - `/connectors` and the unfiltered `/runs` list.
    Those never touch `.source`, so this only exists to satisfy the type."""

    name = "none"

    async def fetch(self, *, limit: int | None = None) -> list[RawMessage]:
        return []


def get_message_source(platform: Platform | None = None) -> MessageSource:
    """The connector one platform's ingestion pipeline pulls from.

    `platform` is resolved from the request the same way any other path or
    query parameter is - FastAPI matches it by name against whatever the route
    this dependency is used from declares (a path param for `/runs/{platform}`
    and `/config/{platform}`, the optional query param for `/runs`). Routes
    with no `platform` at all - `/connectors` - get `None`, which is fine
    since those never call `.run()`. Swapping in a real Slack/GitHub/Teams
    connector is a change to `_MOCK_SOURCES`, not to any route.
    """
    if platform is None:
        return _NoConnector()
    try:
        return _MOCK_SOURCES[platform]()
    except KeyError:
        raise NotFoundError(
            f"No connector configured for platform '{platform.value}'.",
            details={"platform": platform.value},
        ) from None


MessageSourceDep = Annotated[MessageSource, Depends(get_message_source)]


def get_ingestion_service(
    uow: UowDep,
    principal: CurrentPrincipal,
    source: MessageSourceDep,
    llm: LLMDep,
    embeddings: EmbeddingDep,
    settings: SettingsDep,
) -> IngestionService:
    return IngestionService(
        uow=uow,
        principal=principal,
        source=source,
        llm=llm,
        embeddings=embeddings,
        settings=settings,
    )


IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]


def get_insights_service(
    uow: UowDep,
    principal: CurrentPrincipal,
    llm: LLMDep,
    settings: SettingsDep,
) -> UserInsightsService:
    return UserInsightsService(uow, principal, llm, settings)


InsightsServiceDep = Annotated[UserInsightsService, Depends(get_insights_service)]
