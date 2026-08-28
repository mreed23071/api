"""v1 wire contracts for the directory: people, their accounts, their notes.

Wire schemas are the API's public shape and the domain DTOs are its internal
one, deliberately kept apart. Renaming a domain field is a refactor; renaming a
field here breaks every client, so the mapping between them is written out by
hand in the `from_*` classmethods rather than inferred.

The vocabulary here is `user`, matching the table, the paths (`/users`) and the
schemas that already exist - even though the domain layer calls the same idea a
`Person`. That is the seam, not an accident: the console is free to speak of
people while the API speaks of users.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.api.v1.schemas.identity import UserRelationRead
from app.domains.identity.dto import (
    AccountDeletionResult,
    ForgetResult,
    PersonView,
    UnlinkedAccountView,
)
from app.domains.identity.models import PersonNote, Platform, User


class UserDetail(BaseModel):
    """One person's full record, as the profile screen shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    display_name: str | None = None
    job_title: str | None = None
    address: str | None = None
    employment_start: date | None = None
    employment_end: date | None = None
    timezone: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, user: User) -> "UserDetail":
        """Build from an ORM row. `from_attributes` reads them by attribute."""
        return cls.model_validate(user)


class UserListItem(UserDetail):
    """A person plus the counts the directory list shows beside them.

    Extends `UserDetail` rather than repeating it, so a field added to the
    profile appears in the list automatically instead of being forgotten.
    """

    platforms: list[Platform] = Field(
        default_factory=list, description="Platforms this person has an account on."
    )
    message_count: int = 0
    last_message_at: datetime | None = None
    department_id: uuid.UUID | None = Field(
        default=None, description="The department they are filed into, if any."
    )

    @classmethod
    def from_view(cls, view: PersonView) -> "UserListItem":
        """Flatten the domain view - a person plus three aggregates - into one row."""
        return cls(
            **UserDetail.from_entity(view.user).model_dump(),
            platforms=view.platforms,
            message_count=view.message_count,
            last_message_at=view.last_message_at,
            department_id=view.department_id,
        )


class UserCreate(BaseModel):
    """A person created by hand rather than discovered by a connector."""

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    display_name: str | None = Field(default=None, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=64)
    employment_start: date | None = None


class UserUpdate(BaseModel):
    """A partial update. Every field is optional; omitted ones are left alone.

    An omitted field and a field sent as null are different requests, and this
    shape can only express the first. Nothing on a person is meaningfully
    cleared by an edit - a job title is emptied by sending `""` - so that is
    sufficient here, unlike the department patch where "no parent" is a real
    value and needs its own flag.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=64)
    employment_start: date | None = None
    employment_end: date | None = None
    is_active: bool | None = None


class ForgetUserResponse(BaseModel):
    """What erasure removed. The only record that survives the cascade."""

    deleted_messages: int
    deleted_accounts: int

    @classmethod
    def from_result(cls, result: ForgetResult) -> "ForgetUserResponse":
        return cls(
            deleted_messages=result.deleted_messages,
            deleted_accounts=result.deleted_accounts,
        )


class UnlinkedAccountRead(UserRelationRead):
    """An unattributed account, with the context needed to claim it.

    The counts are the point: deciding whose Slack handle this is remains
    guesswork without knowing how much it has said and when it last spoke.
    """

    message_count: int = 0
    last_seen_at: datetime | None = None

    @classmethod
    def from_view(cls, view: UnlinkedAccountView) -> "UnlinkedAccountRead":
        return cls(
            **UserRelationRead.from_entity(view.relation).model_dump(),
            message_count=view.message_count,
            last_seen_at=view.last_seen_at,
        )


class AccountCreate(BaseModel):
    """An external account attached to a person by hand.

    No `external_id`: nobody has authenticated against this account, so the
    platform has not issued one. The server synthesises a unique placeholder.
    """

    user_id: uuid.UUID
    platform: Platform
    external_handle: str = Field(min_length=1, max_length=255)
    external_email: EmailStr | None = None


class AccountDeleteResponse(BaseModel):
    """What deleting an account destroyed along with it."""

    deleted_messages: int

    @classmethod
    def from_result(cls, result: AccountDeletionResult) -> "AccountDeleteResponse":
        return cls(deleted_messages=result.deleted_messages)


class NoteRead(BaseModel):
    """A note somebody wrote about a person."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    author: str
    body: str
    created_at: datetime

    @classmethod
    def from_entity(cls, note: PersonNote) -> "NoteRead":
        return cls.model_validate(note)


class NoteCreate(BaseModel):
    """A new observation about a person."""

    body: str = Field(min_length=1, description="The note itself.")
    author: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Who wrote it. Supplied by the client until operators are rows in "
            "their own right, at which point it comes from the caller instead."
        ),
    )


class DeletedResponse(BaseModel):
    """Confirms which record a delete removed, so a client can reconcile."""

    id: uuid.UUID
