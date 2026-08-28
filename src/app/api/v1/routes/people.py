"""v1 HTTP surface for the directory: people and the notes kept about them.

Every route here is unauthenticated for now. That is a deliberate, temporary
state declared in one place - `app.core.security.provisional` - and asserted by
`tests/contract/test_auth_matrix.py`, which fails if a route becomes open
without being declared.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import DirectoryServiceDep
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.directory import (
    DeletedResponse,
    ForgetUserResponse,
    NoteCreate,
    NoteRead,
    UserCreate,
    UserDetail,
    UserListItem,
    UserUpdate,
)
from app.api.v1.schemas.identity import UserRelationRead
from app.api.v1.schemas.messaging import MessageRead
from app.core.pagination import MAX_LIMIT, PageParams
from app.domains.identity.dto import NewPerson, PersonPatch

router = APIRouter(prefix="/users", tags=["directory"], responses=AUTH_RESPONSES)

UserId = Annotated[uuid.UUID, Path(description="The person's id.")]


@router.get(
    "",
    response_model=list[UserListItem],
    summary="List everyone in the directory",
    description=(
        "Returns every person with the counts the directory shows beside them: "
        "which platforms they have accounts on, how many messages are attributed "
        "to them, when they last said anything, and which department they are "
        "filed into.\n\n"
        "Unpaged by default - the roster is bounded by headcount, and most "
        "callers (the org chart, sender-name lookups, the account-linking "
        "picker) need it complete rather than one page of it. Pass `limit` "
        "(and optionally `offset`) to page through it instead, for a directory "
        "screen that would rather not render the whole roster at once; the "
        "total row count and whether more remains are reported on the "
        "`X-Total-Count` / `X-Has-More` response headers, since the body stays "
        "the same flat array either way."
    ),
)
async def list_users(
    service: DirectoryServiceDep,
    response: Response,
    limit: Annotated[
        int | None, Query(ge=1, le=MAX_LIMIT, description="Page size. Omit for everyone.")
    ] = None,
    offset: Annotated[
        int, Query(ge=0, description="Items to skip. Ignored unless limit is set.")
    ] = 0,
) -> list[UserListItem]:
    if limit is None:
        return [UserListItem.from_view(view) for view in await service.list_people()]

    page = await service.list_people_page(PageParams(limit=limit, offset=offset))
    response.headers["X-Total-Count"] = str(page.total)
    response.headers["X-Has-More"] = "true" if page.has_more else "false"
    return [UserListItem.from_view(view) for view in page.items]


@router.post(
    "",
    response_model=UserDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a person by hand",
    description=(
        "For people a connector has not discovered. The email address is "
        "normalised to lower case before it is checked for duplicates, because "
        "it is the key identity resolution merges on.\n\n"
        "Returns 409 if somebody already holds that address."
    ),
)
async def create_user(body: UserCreate, service: DirectoryServiceDep) -> UserDetail:
    person = await service.create_person(
        NewPerson(
            full_name=body.full_name,
            email=body.email,
            display_name=body.display_name,
            job_title=body.job_title,
            address=body.address,
            timezone=body.timezone,
            employment_start=body.employment_start,
        )
    )
    return UserDetail.from_entity(person)


@router.get(
    "/{user_id}",
    response_model=UserDetail,
    summary="Fetch one person's record",
)
async def get_user(user_id: UserId, service: DirectoryServiceDep) -> UserDetail:
    return UserDetail.from_entity(await service.get_person(user_id))


@router.patch(
    "/{user_id}",
    response_model=UserDetail,
    summary="Update a person's record",
    description=(
        "A partial update: only the fields present in the body are changed. "
        "Clear a text field by sending an empty string rather than null."
    ),
)
async def update_user(
    user_id: UserId, body: UserUpdate, service: DirectoryServiceDep
) -> UserDetail:
    person = await service.update_person(
        user_id,
        PersonPatch(
            full_name=body.full_name,
            display_name=body.display_name,
            job_title=body.job_title,
            address=body.address,
            timezone=body.timezone,
            employment_start=body.employment_start,
            employment_end=body.employment_end,
            is_active=body.is_active,
        ),
    )
    return UserDetail.from_entity(person)


@router.delete(
    "/{user_id}",
    response_model=ForgetUserResponse,
    summary="Erase a person and everything retained about them",
    description=(
        "Right to be forgotten. Removes the person, their linked accounts, their "
        "notes and every message attributed to them. Irreversible.\n\n"
        "The response counts what was destroyed, and is the only record that "
        "survives the deletion - worth keeping wherever the request came from."
    ),
)
async def forget_user(
    user_id: UserId, service: DirectoryServiceDep
) -> ForgetUserResponse:
    return ForgetUserResponse.from_result(await service.forget_person(user_id))


@router.get(
    "/{user_id}/accounts",
    response_model=list[UserRelationRead],
    summary="List the external accounts attributed to one person",
)
async def list_user_accounts(
    user_id: UserId, service: DirectoryServiceDep
) -> list[UserRelationRead]:
    return [
        UserRelationRead.from_entity(relation)
        for relation in await service.list_accounts_for(user_id)
    ]


@router.get(
    "/{user_id}/messages",
    response_model=list[MessageRead],
    summary="List the messages attributed to one person",
)
async def list_user_messages(
    user_id: UserId, service: DirectoryServiceDep
) -> list[MessageRead]:
    return [
        MessageRead.from_entity(message)
        for message in await service.list_messages_for(user_id)
    ]


@router.get(
    "/{user_id}/notes",
    response_model=list[NoteRead],
    summary="List the notes written about one person",
)
async def list_user_notes(
    user_id: UserId, service: DirectoryServiceDep
) -> list[NoteRead]:
    return [NoteRead.from_entity(note) for note in await service.list_notes(user_id)]


@router.post(
    "/{user_id}/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Write a note about a person",
    description=(
        "Notes are what a human observed, kept separate from the generated "
        "summaries: those are derived and can be rebuilt, these cannot."
    ),
)
async def create_user_note(
    user_id: UserId, body: NoteCreate, service: DirectoryServiceDep
) -> NoteRead:
    return NoteRead.from_entity(
        await service.add_note(user_id, body.body, body.author)
    )


notes_router = APIRouter(prefix="/notes", tags=["directory"], responses=AUTH_RESPONSES)


@notes_router.delete(
    "/{note_id}",
    response_model=DeletedResponse,
    summary="Delete a note",
)
async def delete_note(
    note_id: Annotated[uuid.UUID, Path(description="The note's id.")],
    service: DirectoryServiceDep,
) -> DeletedResponse:
    return DeletedResponse(id=await service.delete_note(note_id))
