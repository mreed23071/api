"""v1 HTTP surface for external accounts.

The distinction to hold on to: **unlink** keeps a message and forgets who sent
it; **delete** keeps nothing. Both are here, and they are separate routes
because conflating them is how a support action becomes a data loss incident.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Path, status

from app.api.deps import DirectoryServiceDep
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.directory import (
    AccountCreate,
    AccountDeleteResponse,
    UnlinkedAccountRead,
)
from app.api.v1.schemas.identity import UserRelationRead
from app.domains.identity.dto import NewAccount

router = APIRouter(prefix="/accounts", tags=["directory"], responses=AUTH_RESPONSES)

AccountId = Annotated[uuid.UUID, Path(description="The account's id.")]


@router.get(
    "/unlinked",
    response_model=list[UnlinkedAccountRead],
    summary="List accounts nobody has been attributed to",
    description=(
        "External identities ingestion has seen but that no person has been "
        "matched to yet. Each carries how many messages it has sent and when it "
        "last spoke, which is what makes attributing it a decision rather than a "
        "guess."
    ),
)
async def list_unlinked_accounts(
    service: DirectoryServiceDep,
) -> list[UnlinkedAccountRead]:
    return [
        UnlinkedAccountRead.from_view(view)
        for view in await service.list_unlinked_accounts()
    ]


@router.post(
    "",
    response_model=UserRelationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Attach an external account to a person by hand",
    description=(
        "For accounts a connector has not observed. The server synthesises the "
        "external id, because nobody has authenticated against this account and "
        "the platform has therefore not issued one.\n\n"
        "The first account attached to somebody becomes their primary; later "
        "ones do not displace it."
    ),
)
async def create_account(
    body: AccountCreate, service: DirectoryServiceDep
) -> UserRelationRead:
    relation = await service.create_account(
        NewAccount(
            user_id=body.user_id,
            platform=body.platform,
            external_handle=body.external_handle,
            external_email=body.external_email,
        )
    )
    return UserRelationRead.from_entity(relation)


@router.post(
    "/{account_id}/link",
    response_model=UserRelationRead,
    summary="Attribute an account to a person",
    description=(
        "Every message that ever arrived on this account is reattributed to that "
        "person. The record of which account each message actually came from is "
        "untouched, so provenance survives the move."
    ),
)
async def link_account(
    account_id: AccountId,
    service: DirectoryServiceDep,
    user_id: Annotated[uuid.UUID, Body(embed=True, description="Who to attribute it to.")],
) -> UserRelationRead:
    return UserRelationRead.from_entity(
        await service.link_account(account_id, user_id)
    )


@router.post(
    "/{account_id}/unlink",
    response_model=UserRelationRead,
    summary="Detach an account from its person",
    description=(
        "The messages stay and return to the unresolved pool. Use this when an "
        "account was attributed to the wrong person - the history is preserved "
        "and can be reattributed."
    ),
)
async def unlink_account(
    account_id: AccountId, service: DirectoryServiceDep
) -> UserRelationRead:
    return UserRelationRead.from_entity(await service.unlink_account(account_id))


@router.delete(
    "/{account_id}",
    response_model=AccountDeleteResponse,
    summary="Delete an account and every message that arrived on it",
    description=(
        "Destructive, and distinct from unlinking: this keeps nothing. The "
        "response counts the messages destroyed alongside it."
    ),
)
async def delete_account(
    account_id: AccountId, service: DirectoryServiceDep
) -> AccountDeleteResponse:
    return AccountDeleteResponse.from_result(await service.delete_account(account_id))
