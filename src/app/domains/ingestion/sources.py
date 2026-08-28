"""Message source connectors.

`MessageSource` is the port. A real Slack/GitHub/Teams connector implements this
protocol and is swapped in through the dependency in `app.api.deps` - the
pipeline itself never changes. Everything in this module is mock, deliberately
- see `get_message_source` for where a real connector plugs in.

What a real connector will additionally need, and these mocks deliberately do
not model: an incremental cursor (a watermark per channel or per repository) so
each run fetches only what is new. That belongs on this protocol as a
`fetch(since: Cursor)` argument, plus a table to persist the cursor.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from app.domains.identity.models import Platform
from app.domains.ingestion.dto import RawMessage


@runtime_checkable
class MessageSource(Protocol):
    """Port every connector implements."""

    name: str

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        """Pull recent messages from the platform, newest first.

        The `...` body is the whole point of a Protocol: this declares the shape
        a connector must have without providing any behaviour. A real Slack or
        GitHub connector satisfies it simply by having a matching method - there
        is nothing to inherit and nothing to register.
        """
        ...


_AUTHORS = {
    "U-ALICE": ("alice", "alice@example.com", "Alice Nguyen"),
    "U-BEN": ("benh", "ben@example.com", "Ben Hartley"),
    "U-CARLA": ("carla-dev", "carla@example.com", "Carla Moreau"),
}

#: Plain conversational messages, one list shared by every chat-shaped mock
#: (Slack and Teams today). Real content, not filler - it is what the
#: idempotency and filtering tests are written against.
_CHAT_SEED: list[tuple[Platform, str, str]] = [
    (
        Platform.SLACK,
        "U-ALICE",
        "Staging deploy of the billing service is green, rolling to production after the 3pm change window.",
    ),
    (Platform.SLACK, "U-ALICE", "Anyone up for pizza and beers after work on Friday? My treat."),
    (
        Platform.SLACK,
        "U-BEN",
        "Client escalation on the invoice export: their finance team needs the CSV schema frozen before the contract renewal.",
    ),
    (
        Platform.TEAMS,
        "U-CARLA",
        "My dentist appointment ran long, I will be late to standup, sorry.",
    ),
    (
        Platform.TEAMS,
        "U-ALICE",
        "Roadmap review moved to Thursday. Agenda: Q3 milestones, hiring budget, and the API rate-limit spec.",
    ),
    (Platform.SLACK, "U-BEN", "Happy birthday Carla! The cake is in the kitchen."),
    (
        Platform.SLACK,
        "U-CARLA",
        "The nightly ingestion job hit the 30s timeout again - I think the embedding batch size needs tuning in staging.",
    ),
    (
        Platform.TEAMS,
        "U-BEN",
        "Reminder: onboarding session for the new client is tomorrow at 10, the handover doc is attached to the ticket.",
    ),
    (Platform.SLACK, "U-CARLA", "Watched the new season last night, no spoilers please."),
]


class MockChatSource:
    """Deterministic mock connector for a plain conversational platform.

    One class, not one per platform: Slack and Teams are the same shape here -
    people talking in channels - so the only thing that varies is which
    platform's slice of `_CHAT_SEED` gets served and what the rows are tagged
    with. Fixed ids and a fixed ordering, so a run is reproducible and the
    idempotency guarantee is testable: run it twice, get zero new rows.
    """

    def __init__(self, platform: Platform, *, name: str, now: datetime | None = None) -> None:
        self.platform = platform
        self.name = name
        self._now = now or datetime.now(UTC)

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        """Return this platform's fixed slice of `_CHAT_SEED`, newest first."""
        seed = [
            (author_id, content)
            for platform, author_id, content in _CHAT_SEED
            if platform == self.platform
        ]
        messages: list[RawMessage] = []
        for index, (author_id, content) in enumerate(seed):
            handle, email, display_name = _AUTHORS[author_id]
            messages.append(
                RawMessage(
                    external_message_id=f"{self.platform.value}-{index + 1:04d}",
                    platform=self.platform,
                    external_author_id=author_id,
                    author_handle=handle,
                    author_email=email,
                    author_display_name=display_name,
                    conversation_id=f"{self.platform.value}-general",
                    content=content,
                    sent_at=self._now - timedelta(hours=len(seed) - index),
                    metadata={"source": self.name, "seed_index": index},
                )
            )
        return messages[:limit] if limit else messages


#: Commits are a different shape from a chat message, not a variant of one -
#: `message` here becomes the commit message, the rest becomes
#: `metadata["commit"]`, matching `CommitDetail`/`CommitFile` in
#: `api/v1/schemas/messaging.py`. Repository names match what's already in the
#: seeded ingestion-run fixture data, for consistency.
_COMMIT_SEED: list[dict[str, Any]] = [
    {
        "author_id": "U-BEN",
        "repository": "threadline/api",
        "branch": "main",
        "sha": "3691593a7c1e4f0b9d2a6c8e1f4b7d0a2c5e8f1b",
        "message": "Add migration for the messages embedding column",
        "files": [
            {
                "path": "migrations/versions/0002_add_embedding.py",
                "status": "added",
                "additions": 42,
                "deletions": 0,
            },
        ],
        "additions": 42,
        "deletions": 0,
    },
    {
        "author_id": "U-CARLA",
        "repository": "threadline/infra",
        "branch": "main",
        "sha": "b324dca9f2e5c7a1b4d6f8e0a3c5b7d9f1e3a5c7",
        "message": "Revert the rollback commit; postmortem action items are in the backlog",
        "files": [
            {"path": "deploy/rollback.sh", "status": "modified", "additions": 3, "deletions": 18},
        ],
        "additions": 3,
        "deletions": 18,
    },
    {
        "author_id": "U-ALICE",
        "repository": "threadline/console",
        "branch": "main",
        "sha": "f8c2b57d1a3e6c9b2d4f7a0c3e6b9d1f4a7c0e3b",
        "message": "Address review comments on the schema PR: rename column, add HNSW index",
        "files": [
            {
                "path": "src/lib/api/types.ts",
                "status": "modified",
                "additions": 6,
                "deletions": 2,
            },
            {
                "path": "migrations/versions/0002_add_embedding.py",
                "status": "modified",
                "additions": 4,
                "deletions": 1,
            },
        ],
        "additions": 10,
        "deletions": 3,
    },
    {
        "author_id": "U-BEN",
        "repository": "threadline/ingestion-worker",
        "branch": "feature/batch-tuning",
        "sha": "1155688e4b7d0a2c5f8b1e4d7a0c3f6b9e2d5a8c",
        "message": "Tune embedding batch size after nightly job hit the 30s timeout",
        "files": [
            {"path": "src/worker/config.py", "status": "modified", "additions": 5, "deletions": 5},
        ],
        "additions": 5,
        "deletions": 5,
    },
    {
        "author_id": "U-CARLA",
        "repository": "threadline/connectors",
        "branch": "main",
        "sha": "b0186bd3e6a9c2f5b8d1e4a7c0f3b6d9e2a5c8f1",
        "message": "Fix pagination cursor for the Slack channel history connector",
        "files": [
            {
                "path": "src/connectors/slack.py",
                "status": "modified",
                "additions": 11,
                "deletions": 4,
            },
        ],
        "additions": 11,
        "deletions": 4,
    },
    {
        "author_id": "U-ALICE",
        "repository": "threadline/api",
        "branch": "main",
        "sha": "74c34362f9a1c4e7b0d3f6a9c2e5b8d1f4a7c0e3",
        "message": "Backfill filter_prompt_version on historical decisions",
        "files": [
            {
                "path": "migrations/versions/0003_persist_is_fallback.py",
                "status": "added",
                "additions": 28,
                "deletions": 0,
            },
        ],
        "additions": 28,
        "deletions": 0,
    },
]


class MockGitHubCommitSource:
    """Deterministic mock GitHub connector - commits, not chat messages.

    Fixed ids and a fixed ordering, same as `MockChatSource`, so ingestion
    stays idempotent and testable. Each row's `kind` is `"commit"`, and the
    diff shape (sha, files touched, additions/deletions) lives in
    `metadata["commit"]` rather than in `content` - `content` is the commit
    message, which is what the filtering agent actually reads.
    """

    name = "github-mock"

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        messages: list[RawMessage] = []
        for index, commit in enumerate(_COMMIT_SEED):
            handle, email, display_name = _AUTHORS[commit["author_id"]]
            messages.append(
                RawMessage(
                    external_message_id=f"{commit['repository']}@{commit['sha'][:7]}",
                    platform=Platform.GITHUB,
                    external_author_id=commit["author_id"],
                    author_handle=handle,
                    author_email=email,
                    author_display_name=display_name,
                    conversation_id=commit["repository"],
                    content=commit["message"],
                    sent_at=self._now - timedelta(hours=len(_COMMIT_SEED) - index),
                    kind="commit",
                    metadata={
                        "source": self.name,
                        "seed_index": index,
                        "commit": {
                            "sha": commit["sha"],
                            "repository": commit["repository"],
                            "branch": commit["branch"],
                            "url": (
                                f"https://github.com/{commit['repository']}"
                                f"/commit/{commit['sha']}"
                            ),
                            "files": commit["files"],
                            "additions": commit["additions"],
                            "deletions": commit["deletions"],
                        },
                    },
                )
            )
        return messages[:limit] if limit else messages
