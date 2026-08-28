"""Data access for the messaging context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db.repository import Repository
from app.core.pagination import PageParams, Paginated
from app.domains.identity.models import Platform
from app.domains.messaging.dto import MessageFilters, NewMessage
from app.domains.messaging.models import Message

#: Characters that mean something to SQL's LIKE and must be neutralised in a
#: user-supplied search term.
_LIKE_WILDCARDS = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _contains(term: str) -> str:
    """Turn a search box's contents into a safe LIKE pattern.

    `%` and `_` are wildcards to SQL - `%` matches anything, `_` matches one
    character - so a user searching for "50%" would otherwise get a query that
    matches far more than they asked for. Escaping them first means the term is
    treated as literal text.

    This is about correctness, not injection: the value is still sent as a bound
    parameter, so it can never be executed as SQL either way.
    """
    return f"%{term.strip().translate(_LIKE_WILDCARDS)}%"

#: Name of the constraint that makes ingestion idempotent. Referenced by the
#: upsert and asserted by an integration test, so a rename cannot silently turn
#: the cron endpoint into a duplicate-writer.
IDEMPOTENCY_CONSTRAINT = "uq_messages_platform_external_message_id"


class MessageRepository(Repository):
    """Reads and writes rows in the `messages` table."""

    async def bulk_upsert(self, messages: Sequence[NewMessage]) -> list[uuid.UUID]:
        """Insert a batch, ignoring anything already ingested.

        The scheduler is at-least-once by nature (retries, overlapping windows),
        so `(platform, external_message_id)` is the idempotency key and the
        conflict is swallowed rather than raised. Returns the ids actually
        written, which is how the run report distinguishes new from duplicate.
        """
        if not messages:
            return []

        rows = [message.model_dump(mode="python") for message in messages]
        statement = (
            pg_insert(Message)
            .values(rows)
            .on_conflict_do_nothing(constraint=IDEMPOTENCY_CONSTRAINT)
            .returning(Message.id)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def existing_keys(
        self, keys: Sequence[tuple[Platform, str]]
    ) -> set[tuple[Platform, str]]:
        """Which of these messages do we already hold?

        Matches on the full composite key in SQL rather than filtering in
        Python, so two platforms with overlapping id formats cannot cause an
        over-fetch.
        """
        if not keys:
            return set()

        conditions = [
            (Message.platform == platform) & (Message.external_message_id == external_id)
            for platform, external_id in keys
        ]
        clause = conditions[0]
        for condition in conditions[1:]:
            clause = clause | condition

        statement = self.scoped(
            select(Message.platform, Message.external_message_id), Message
        ).where(clause)
        rows = (await self.session.execute(statement)).all()
        return {(platform, external_id) for platform, external_id in rows}

    async def latest_for_users(
        self,
        user_ids: Sequence[uuid.UUID],
        *,
        per_user_limit: int = 25,
    ) -> dict[uuid.UUID, list[Message]]:
        """Most recent N messages per user, in a single round trip.

        A window function keeps this at one query no matter how many users are
        on the page. The alternative - a query per user - is the N+1 that makes
        the summarization endpoint collapse as soon as page size grows.
        """
        if not user_ids:
            return {}

        ranked = (
            self.scoped(
                select(
                    Message,
                    func.row_number()
                    .over(
                        partition_by=Message.sender_user_id,
                        order_by=(Message.sent_at.desc(), Message.id.desc()),
                    )
                    .label("rank"),
                ),
                Message,
            )
            .where(Message.sender_user_id.in_(user_ids))
            .subquery()
        )

        statement = (
            select(Message)
            .join(ranked, Message.id == ranked.c.id)
            .where(ranked.c.rank <= per_user_limit)
            .order_by(Message.sender_user_id, Message.sent_at.desc())
        )

        grouped: dict[uuid.UUID, list[Message]] = {user_id: [] for user_id in user_ids}
        for message in (await self.session.execute(statement)).scalars().all():
            grouped[message.sender_user_id].append(message)
        return grouped

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        """How many messages one person has. Counted in the database, not loaded."""
        statement = self.scoped(
            select(func.count()).select_from(Message), Message
        ).where(Message.sender_user_id == user_id)
        return int((await self.session.execute(statement)).scalar_one())

    # -- attribution --------------------------------------------------------
    #
    # Linking an account to a person reattributes every message that arrived on
    # it; unlinking returns them to the unresolved pool. Both are a single
    # UPDATE rather than a read-modify-write loop: the whole point of holding
    # `sender_relation_id` alongside `sender_user_id` is that provenance never
    # moves, so only one column ever changes.

    async def reattribute(
        self, relation_id: uuid.UUID, user_id: uuid.UUID | None
    ) -> int:
        """Point every message from one account at a person, or at nobody.

        One `UPDATE ... WHERE sender_relation_id = ...` regardless of how many
        messages there are, rather than loading them and saving them back.
        Passing `None` for `user_id` is the unlink direction: the messages stay,
        but no longer belong to anyone.

        `sender_relation_id` is deliberately untouched. That column records
        which account a message actually arrived on, and it must survive
        every re-attribution or provenance is lost.

        Returns the number of rows changed, which the caller logs.
        """
        result = await self.session.execute(
            update(Message)
            .where(Message.sender_relation_id == relation_id)
            .values(sender_user_id=user_id)
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    # -- aggregates, batched --------------------------------------------
    #
    # One query each, for every person or account at once. The per-row variants
    # are deliberately absent: a directory of N people asking N times is the
    # shape that looks fine with twelve rows and falls over at a thousand.

    async def counts_by_user(
        self, user_ids: Sequence[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, int]:
        """How many messages each person has, as one query.

        Emits `SELECT sender_user_id, count(*) ... GROUP BY sender_user_id`, so
        the database does the counting. The dict comprehension at the end -
        `{k: v for k, v in rows.all()}` - turns the result rows into a lookup
        table the caller can index by user id.

        People with no messages are simply absent from the dict, which is why
        callers use `.get(user_id, 0)` rather than `[user_id]`.

        `user_ids`, when given, scopes the `GROUP BY` to those people instead
        of the whole table - what a paginated caller wants, since its query
        cost should track the page size, not the size of the roster.
        """
        statement = (
            self.scoped(select(Message.sender_user_id, func.count()), Message)
            .where(Message.sender_user_id.is_not(None))
            .group_by(Message.sender_user_id)
        )
        if user_ids is not None:
            statement = statement.where(Message.sender_user_id.in_(user_ids))
        rows = await self.session.execute(statement)
        return {user_id: int(count) for user_id, count in rows.all()}

    async def last_sent_by_user(
        self, user_ids: Sequence[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, datetime]:
        """When each person last sent anything, as one query.

        Same shape as `counts_by_user`, but `max(sent_at)` instead of `count`
        - including the same optional `user_ids` scoping.
        """
        statement = (
            self.scoped(select(Message.sender_user_id, func.max(Message.sent_at)), Message)
            .where(Message.sender_user_id.is_not(None))
            .group_by(Message.sender_user_id)
        )
        if user_ids is not None:
            statement = statement.where(Message.sender_user_id.in_(user_ids))
        rows = await self.session.execute(statement)
        return {user_id: sent_at for user_id, sent_at in rows.all()}

    async def counts_by_platform(self) -> dict[Platform, int]:
        """How many messages each platform has contributed."""
        statement = self.scoped(
            select(Message.platform, func.count()), Message
        ).group_by(Message.platform)
        rows = await self.session.execute(statement)
        return {platform: int(count) for platform, count in rows.all()}

    async def last_sent_by_platform(self) -> dict[Platform, datetime]:
        """When each platform last delivered anything."""
        statement = self.scoped(
            select(Message.platform, func.max(Message.sent_at)), Message
        ).group_by(Message.platform)
        rows = await self.session.execute(statement)
        return {platform: sent_at for platform, sent_at in rows.all()}

    async def counts_by_relation(self) -> dict[uuid.UUID, int]:
        """How many messages arrived on each external account.

        Grouped by the account rather than the person, which is the question the
        unlinked-accounts screen asks: an unattributed account has no person to
        count against.
        """
        statement = (
            self.scoped(select(Message.sender_relation_id, func.count()), Message)
            .where(Message.sender_relation_id.is_not(None))
            .group_by(Message.sender_relation_id)
        )
        rows = await self.session.execute(statement)
        return {relation_id: int(count) for relation_id, count in rows.all()}

    async def last_sent_by_relation(self) -> dict[uuid.UUID, datetime]:
        """When each external account last sent anything."""
        statement = (
            self.scoped(
                select(Message.sender_relation_id, func.max(Message.sent_at)), Message
            )
            .where(Message.sender_relation_id.is_not(None))
            .group_by(Message.sender_relation_id)
        )
        rows = await self.session.execute(statement)
        return {relation_id: sent_at for relation_id, sent_at in rows.all()}

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[Message]:
        """Every message attributed to one person, newest first."""
        statement = (
            self.scoped(select(Message), Message)
            .where(Message.sender_user_id == user_id)
            .order_by(Message.sent_at.desc())
        )
        return (await self.session.execute(statement)).scalars().all()

    # -- removal ------------------------------------------------------------

    async def delete_for_relation(self, relation_id: uuid.UUID) -> int:
        """Delete every message that arrived on one account."""
        result = await self.session.execute(
            delete(Message).where(Message.sender_relation_id == relation_id)
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def delete_for_user(self, user_id: uuid.UUID) -> int:
        """Delete every message attributed to one person - the erasure path."""
        result = await self.session.execute(
            delete(Message).where(Message.sender_user_id == user_id)
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    # -- browsing -----------------------------------------------------------

    def _filtered(self, filters: MessageFilters) -> Select[tuple[Message]]:
        """The base query every read in this section starts from.

        Factored out so the scoping hook and every filter are applied
        identically to both the windowed query and `count`, which must count
        exactly the rows `search` would return.
        """
        statement = self.scoped(select(Message), Message)

        if filters.user_id is not None:
            statement = statement.where(Message.sender_user_id == filters.user_id)
        if filters.platform is not None:
            statement = statement.where(Message.platform == filters.platform)
        if filters.category is not None:
            statement = statement.where(Message.filter_category == filters.category)
        if filters.sent_from is not None:
            statement = statement.where(Message.sent_at >= filters.sent_from)
        if filters.sent_to is not None:
            statement = statement.where(Message.sent_at <= filters.sent_to)
        if filters.search:
            statement = statement.where(
                Message.content.ilike(_contains(filters.search), escape="\\")
            )
        return statement

    async def search(self, filters: MessageFilters, params: PageParams) -> Paginated[Message]:
        """One page of messages matching a filter set, newest first, plus the total.

        `sent_at` alone is not unique, so a tie-break on `id` keeps the paging
        stable - without it, two messages sent in the same instant could each
        appear on both of two adjacent pages, or on neither.
        """
        statement = (
            self._filtered(filters)
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(params.limit)
            .offset(params.offset)
        )
        items = (await self.session.execute(statement)).scalars().all()
        total = await self.count(filters)
        return Paginated(items=items, total=total, params=params)

    async def count(self, filters: MessageFilters) -> int:
        """How many messages match, counted in the database rather than loaded."""
        statement = select(func.count()).select_from(self._filtered(filters).subquery())
        return int((await self.session.execute(statement)).scalar_one())
