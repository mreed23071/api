from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.domains.identity.models import Platform, User
from app.domains.ingestion.dto import RawMessage
from app.domains.messaging.models import Message


def make_message(user: User, **overrides: Any) -> Message:
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "kind": "message",
        "sender_user_id": user.id,
        "sender_relation_id": None,
        "platform": Platform.SLACK,
        "external_message_id": f"msg-{uuid.uuid4().hex[:8]}",
        "conversation_id": "slack-general",
        "content": "The deploy is green, rolling to production after the change window.",
        "embedding": None,
        "embedding_model": None,
        "filter_category": "business",
        "filter_reason": "matched business markers",
        "filter_prompt_version": "v1",
        "sent_at": now,
        "source_metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    return Message(**{**defaults, **overrides})


def make_raw_message(**overrides: Any) -> RawMessage:
    defaults: dict[str, Any] = {
        "external_message_id": f"raw-{uuid.uuid4().hex[:8]}",
        "platform": Platform.SLACK,
        "external_author_id": "U-TEST",
        "author_handle": "tester",
        "author_email": "tester@example.com",
        "author_display_name": "Test Person",
        "conversation_id": "slack-general",
        "content": "Sprint review moved to Thursday; the roadmap agenda is attached to the ticket.",
        "sent_at": datetime.now(UTC),
        "metadata": {},
    }
    return RawMessage(**{**defaults, **overrides})
