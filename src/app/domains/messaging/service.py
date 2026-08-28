"""Messaging business logic.

Two audiences share this service, and they are gated differently. The ingestion
pipeline calls `unseen` and `store` under machine scopes; the console calls
`browse` through the provisional console gate. That split is deliberate - the
pipeline's permissions should not widen just because a browsing screen was
added.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.pagination import PageParams, Paginated
from app.core.security.principal import Principal, Scope
from app.core.security.provisional import require_console_access
from app.domains.identity.models import Platform
from app.domains.messaging.dto import MessageFilters, NewMessage
from app.domains.messaging.models import Message
from app.domains.uow import UnitOfWork


class MessageService:
    """Stores messages for the pipeline, and reads them back for the console."""

    def __init__(self, uow: UnitOfWork, principal: Principal) -> None:
        self.uow = uow
        self.principal = principal

    async def unseen(self, keys: Sequence[tuple[Platform, str]]) -> list[tuple[Platform, str]]:
        """Filter a batch down to what we have not stored yet."""
        self.principal.require(Scope.INGEST_RUN)
        known = await self.uow.messages.existing_keys(keys)
        return [key for key in keys if key not in known]

    async def store(self, messages: Sequence[NewMessage]) -> list[uuid.UUID]:
        """Persist a batch. The caller owns the transaction."""
        self.principal.require(Scope.INGEST_RUN)
        return await self.uow.messages.bulk_upsert(messages)

    async def recent_for_users(
        self, user_ids: Sequence[uuid.UUID], *, per_user_limit: int = 25
    ) -> dict[uuid.UUID, list[Message]]:
        """The most recent messages for each of several people, in one query.

        Used by the summariser, which needs a transcript per person and must not
        issue one query per person to get it.
        """
        self.principal.require(Scope.MESSAGES_READ)
        return await self.uow.messages.latest_for_users(user_ids, per_user_limit=per_user_limit)

    async def browse(self, filters: MessageFilters, page: PageParams) -> Paginated[Message]:
        """One page of messages matching a filter set, newest first, plus the total.

        The console's message browser. An empty `MessageFilters()` returns the
        most recent messages unfiltered, which is what the screen shows before
        anyone touches a control.
        """
        require_console_access(self.principal)
        return await self.uow.messages.search(filters, page)

    async def for_person(self, user_id: uuid.UUID) -> Sequence[Message]:
        """Every message attributed to one person, newest first."""
        require_console_access(self.principal)
        return await self.uow.messages.list_for_user(user_id)
