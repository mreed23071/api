"""Composition root.

Everything a route needs is built here from request scope, so routes stay
declarative and every dependency has exactly one override point in tests
(`app.dependency_overrides[get_message_source] = ...`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from app.core.config import Settings, get_settings
from app.core.db.engine import SessionDep
from app.core.pagination import MAX_LIMIT, PageParams
from app.core.security.dependencies import CurrentPrincipal
from app.domains.identity.directory import DirectoryService
from app.domains.ingestion.service import IngestionService
from app.domains.ingestion.sources import MessageSource, MockMessageService
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


def get_message_source() -> MessageSource:
    """The connector the ingestion pipeline pulls from.

    Swapping in a real Slack/GitHub connector is a change to this one function.
    """
    return MockMessageService()


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
