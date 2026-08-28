"""v1 wire contracts for identity."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domains.identity.models import Platform, User, UserRelation


class UserRelationRead(BaseModel):
    """A third-party account linked to an internal user."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    #: Null when the account has not been attributed to anybody yet.
    user_id: uuid.UUID | None = None
    platform: Platform
    external_id: str
    external_handle: str | None = None
    external_email: str | None = None
    is_primary: bool = False
    created_at: datetime

    @classmethod
    def from_entity(cls, relation: UserRelation) -> UserRelationRead:
        return cls.model_validate(relation)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    display_name: str | None = None
    job_title: str | None = None
    timezone: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, user: User) -> UserRead:
        return cls.model_validate(user)


class UserWithRelations(UserRead):
    relations: list[UserRelationRead] = Field(default_factory=list)
