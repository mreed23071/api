from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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


#: The same three fictional people on every platform, so a person who shows up
#: in two of them still resolves to one identity - the product's core claim,
#: and the reason `test_a_person_seen_on_several_platforms_is_one_user`
#: exists. Kept independent of `fixtures-service`'s own seed data: that one
#: exercises wire-shape mapping (see test_sources.py), this one exercises the
#: pipeline downstream of it, and the two should be able to change on their
#: own schedules.
_CHAT_AUTHORS = {
    "U-ALICE": ("alice", "alice@example.com", "Alice Nguyen"),
    "U-BEN": ("benh", "ben@example.com", "Ben Hartley"),
    "U-CARLA": ("carla-dev", "carla@example.com", "Carla Moreau"),
}

_CHAT_SEED: list[tuple[Platform, str, str]] = [
    (
        Platform.SLACK,
        "U-ALICE",
        "Staging deploy of the billing service is green, rolling to production "
        "after the 3pm change window.",
    ),
    (Platform.SLACK, "U-ALICE", "Anyone up for pizza and beers after work on Friday? My treat."),
    (
        Platform.SLACK,
        "U-BEN",
        "Client escalation on the invoice export: their finance team needs the "
        "CSV schema frozen before the contract renewal.",
    ),
    (
        Platform.TEAMS,
        "U-CARLA",
        "My dentist appointment ran long, I will be late to standup, sorry.",
    ),
    (
        Platform.TEAMS,
        "U-ALICE",
        "Roadmap review moved to Thursday. Agenda: Q3 milestones, hiring budget, "
        "and the API rate-limit spec.",
    ),
    (Platform.SLACK, "U-BEN", "Happy birthday Carla! The cake is in the kitchen."),
    (
        Platform.SLACK,
        "U-CARLA",
        "The nightly ingestion job hit the 30s timeout again - I think the embedding "
        "batch size needs tuning in staging.",
    ),
    (
        Platform.TEAMS,
        "U-BEN",
        "Reminder: onboarding session for the new client is tomorrow at 10, "
        "the handover doc is attached to the ticket.",
    ),
    (Platform.SLACK, "U-CARLA", "Watched the new season last night, no spoilers please."),
]


def make_chat_seed(platform: Platform) -> list[RawMessage]:
    """Deterministic, mixed business/personal content for one platform.

    Fixed ids and ordering, so a run is reproducible and the idempotency
    guarantee stays testable: run it twice, get zero new rows.
    """
    now = datetime.now(UTC)
    seed = [(author, content) for p, author, content in _CHAT_SEED if p == platform]
    messages: list[RawMessage] = []
    for index, (author_id, content) in enumerate(seed):
        handle, email, display_name = _CHAT_AUTHORS[author_id]
        messages.append(
            make_raw_message(
                external_message_id=f"{platform.value}-{index + 1:04d}",
                platform=platform,
                external_author_id=author_id,
                author_handle=handle,
                author_email=email,
                author_display_name=display_name,
                conversation_id=f"{platform.value}-general",
                content=content,
                sent_at=now - timedelta(hours=len(seed) - index),
            )
        )
    return messages


_COMMIT_SEED: list[dict[str, Any]] = [
    {
        "author_id": "U-BEN",
        "repository": "mabinsoft/api",
        "branch": "main",
        "sha": "3691593a7c1e4f0b9d2a6c8e1f4b7d0a2c5e8f1b",
        "message": "Add migration for the messages embedding column",
        "files": [
            {
                "path": "migrations/versions/0002_add_embedding.py",
                "status": "added",
                "additions": 42,
                "deletions": 0,
            }
        ],
        "additions": 42,
        "deletions": 0,
    },
    {
        "author_id": "U-CARLA",
        "repository": "mabinsoft/infra",
        "branch": "main",
        "sha": "b324dca9f2e5c7a1b4d6f8e0a3c5b7d9f1e3a5c7",
        "message": "Revert the rollback commit; postmortem action items are in the backlog",
        "files": [
            {"path": "deploy/rollback.sh", "status": "modified", "additions": 3, "deletions": 18}
        ],
        "additions": 3,
        "deletions": 18,
    },
    {
        "author_id": "U-ALICE",
        "repository": "mabinsoft/console",
        "branch": "main",
        "sha": "f8c2b57d1a3e6c9b2d4f7a0c3e6b9d1f4a7c0e3b",
        "message": "Address review comments on the schema PR: rename column, add HNSW index",
        "files": [
            {"path": "src/lib/api/types.ts", "status": "modified", "additions": 6, "deletions": 2}
        ],
        "additions": 6,
        "deletions": 2,
    },
]


def make_commit_seed() -> list[RawMessage]:
    """Deterministic GitHub-shaped `RawMessage`s - `kind="commit"`, with the
    diff metadata `CommitDetail`/`CommitFile` (api/v1/schemas/messaging.py)
    expects. Companion to `make_chat_seed`, for the same reason."""
    now = datetime.now(UTC)
    messages: list[RawMessage] = []
    for index, commit in enumerate(_COMMIT_SEED):
        handle, email, display_name = _CHAT_AUTHORS[commit["author_id"]]
        messages.append(
            make_raw_message(
                external_message_id=f"{commit['repository']}@{commit['sha'][:7]}",
                platform=Platform.GITHUB,
                external_author_id=commit["author_id"],
                author_handle=handle,
                author_email=email,
                author_display_name=display_name,
                conversation_id=commit["repository"],
                content=commit["message"],
                sent_at=now - timedelta(hours=len(_COMMIT_SEED) - index),
                kind="commit",
                metadata={
                    "source": "github-test-seed",
                    "commit": {
                        "sha": commit["sha"],
                        "repository": commit["repository"],
                        "branch": commit["branch"],
                        "url": f"https://github.com/{commit['repository']}/commit/{commit['sha']}",
                        "files": commit["files"],
                        "additions": commit["additions"],
                        "deletions": commit["deletions"],
                    },
                },
            )
        )
    return messages
