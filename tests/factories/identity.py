from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.domains.identity.models import Platform, User, UserRelation


def make_user(**overrides: Any) -> User:
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "email": f"user-{uuid.uuid4().hex[:8]}@example.com",
        "full_name": "Test Person",
        "display_name": "tester",
        "job_title": None,
        "timezone": None,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    user = User(**{**defaults, **overrides})
    # lazy="raise" forbids implicit loading; seed the collections so unit tests
    # can read them without a session.
    user.__dict__.setdefault("relations", [])
    user.__dict__.setdefault("messages", [])
    user.__dict__.setdefault("notes", [])
    return user


def make_relation(user: User, **overrides: Any) -> UserRelation:
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": user.id,
        "platform": Platform.SLACK,
        "external_id": f"U-{uuid.uuid4().hex[:6].upper()}",
        "external_handle": user.display_name,
        "external_email": user.email,
        "is_primary": False,
        "details": {},
        "created_at": now,
        "updated_at": now,
    }
    return UserRelation(**{**defaults, **overrides})
