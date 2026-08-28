"""Message source connectors.

`MessageSource` is the port. A real Slack/GitHub/Teams connector implements this
protocol and is swapped in through the dependency in `app.api.v1.routes` - the
pipeline itself never changes.

What a real connector will additionally need, and this mock deliberately does
not model: an incremental cursor (a watermark per channel or per repository) so
each run fetches only what is new. That belongs on this protocol as a
`fetch(since: Cursor)` argument, plus a table to persist the cursor.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

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

_SEED: list[tuple[Platform, str, str]] = [
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
        Platform.GITHUB,
        "U-BEN",
        "Opened a PR to add the migration for the messages embedding column - needs review before the sprint ends.",
    ),
    (
        Platform.GITHUB,
        "U-CARLA",
        "Reverted the rollback commit; the incident postmortem is in the shared drive and the action items are in the backlog.",
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
        Platform.GITHUB,
        "U-ALICE",
        "Review comments addressed on the schema PR: renamed the column and added the HNSW index.",
    ),
    (
        Platform.TEAMS,
        "U-BEN",
        "Reminder: onboarding session for the new client is tomorrow at 10, the handover doc is attached to the ticket.",
    ),
    (Platform.SLACK, "U-CARLA", "Watched the new season last night, no spoilers please."),
]


class MockMessageService:
    """Deterministic dummy connector used until the real ones are built.

    Fixed ids and a fixed ordering, so an ingestion run is reproducible and the
    idempotency guarantee is testable: run it twice, get zero new rows.
    """

    name = "mock"

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        """Return the fixed fixture set, newest first, optionally truncated.

        The same twelve messages with the same ids every time. That determinism
        is what makes the idempotency guarantee testable: run ingestion twice
        and the second run must store nothing.
        """
        messages: list[RawMessage] = []
        for index, (platform, author_id, content) in enumerate(_SEED):
            handle, email, display_name = _AUTHORS[author_id]
            messages.append(
                RawMessage(
                    external_message_id=f"{platform.value}-{index + 1:04d}",
                    platform=platform,
                    external_author_id=author_id,
                    author_handle=handle,
                    author_email=email,
                    author_display_name=display_name,
                    conversation_id=f"{platform.value}-general",
                    content=content,
                    sent_at=self._now - timedelta(hours=len(_SEED) - index),
                    metadata={"source": self.name, "seed_index": index},
                )
            )
        return messages[:limit] if limit else messages
