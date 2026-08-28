"""Message source doubles."""

from __future__ import annotations

from collections.abc import Sequence

from app.domains.ingestion.dto import RawMessage


class ScriptedMessageSource:
    """Returns exactly the messages a test hands it."""

    name = "scripted"

    def __init__(self, messages: Sequence[RawMessage] | None = None) -> None:
        self.messages = list(messages or [])
        self.fetch_calls = 0

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        self.fetch_calls += 1
        return self.messages[:limit] if limit else self.messages
