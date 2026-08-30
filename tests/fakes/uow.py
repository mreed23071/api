"""An in-memory unit of work.

Lets the whole ingestion and insights pipeline be exercised - branching,
ordering, error handling, the shape of what gets written - without Postgres.
Integration tests then verify the *SQL* separately, against a real pgvector.

The split matters: pipeline logic changes weekly and must be fast to test; the
window function and the upsert change rarely and must be tested for real.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from app.core.pagination import PageParams, Paginated
from app.core.security.principal import TenantContext
from app.domains.identity.models import PersonNote, Platform, User, UserRelation
from app.domains.ingestion.models import IngestionRun
from app.domains.messaging.dto import MessageFilters, NewMessage
from app.domains.messaging.models import Message
from app.domains.organization.models import OrgNode, OrgNodeMember


class FakeUserRepository:
    def __init__(self, users: list[User], cascade=None) -> None:  # type: ignore[no-untyped-def]
        self.users = users
        self.cascade = cascade

    async def list_users(
        self, params: PageParams, *, active_only: bool = True, with_relations: bool = False
    ) -> Paginated[User]:
        matching = [u for u in self.users if u.is_active or not active_only]
        window = matching[params.offset : params.offset + params.limit]
        return Paginated(items=window, total=len(matching), params=params)

    async def count(self, *, active_only: bool = True) -> int:
        return len([u for u in self.users if u.is_active or not active_only])

    async def list_all(self, *, active_only: bool = False) -> list[User]:
        return sorted(
            [u for u in self.users if u.is_active or not active_only],
            key=lambda u: (u.full_name or "", str(u.id)),
        )

    async def get(self, user_id: uuid.UUID) -> User | None:
        return next((u for u in self.users if u.id == user_id), None)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self.users if u.email == email), None)

    async def add(self, user: User) -> User:
        if user.id is None:
            user.id = uuid.uuid4()
        if getattr(user, "created_at", None) is None:
            user.created_at = datetime.now(UTC)
        if getattr(user, "updated_at", None) is None:
            user.updated_at = datetime.now(UTC)
        # Through the instrumented attribute, not user.__dict__[...] directly -
        # see the matching comment in tests/factories/identity.py.
        if "relations" not in user.__dict__:
            user.relations = []
        self.users.append(user)
        return user

    async def remove(self, user: User) -> None:
        """Mirrors ON DELETE CASCADE.

        A fake that removed only the row would make erasure look like it worked
        while leaving the messages behind - the exact bug worth catching here.
        """
        self.users.remove(user)
        if self.cascade is not None:
            self.cascade(user.id)


class FakeUserRelationRepository:
    def __init__(self, relations: list[UserRelation]) -> None:
        self.relations = relations

    async def resolve(self, platform: Platform, external_id: str) -> UserRelation | None:
        return next(
            (r for r in self.relations if r.platform == platform and r.external_id == external_id),
            None,
        )

    async def resolve_many(
        self, identities: Sequence[tuple[Platform, str]]
    ) -> dict[tuple[Platform, str], UserRelation]:
        wanted = set(identities)
        return {
            (r.platform, r.external_id): r
            for r in self.relations
            if (r.platform, r.external_id) in wanted
        }

    async def list_for_user(self, user_id: uuid.UUID) -> list[UserRelation]:
        return [r for r in self.relations if r.user_id == user_id]

    async def list_all(self) -> list[UserRelation]:
        return list(self.relations)

    async def for_users(self, user_ids: Sequence[uuid.UUID]) -> list[UserRelation]:
        wanted = set(user_ids)
        return [r for r in self.relations if r.user_id in wanted]

    async def list_unlinked(self) -> list[UserRelation]:
        return [r for r in self.relations if r.user_id is None]

    async def get(self, relation_id: uuid.UUID) -> UserRelation | None:
        return next((r for r in self.relations if r.id == relation_id), None)

    async def has_primary(self, user_id: uuid.UUID) -> bool:
        return any(r.user_id == user_id and r.is_primary for r in self.relations)

    async def counts_by_platform(self) -> dict[Platform, int]:
        counts: dict[Platform, int] = {}
        for relation in self.relations:
            counts[relation.platform] = counts.get(relation.platform, 0) + 1
        return counts

    async def remove(self, relation: UserRelation) -> None:
        self.relations.remove(relation)

    async def add(self, relation: UserRelation) -> UserRelation:
        if relation.id is None:
            relation.id = uuid.uuid4()
        self.relations.append(relation)
        return relation


class FakeMessageRepository:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.upserted: list[NewMessage] = []

    async def existing_keys(
        self, keys: Sequence[tuple[Platform, str]]
    ) -> set[tuple[Platform, str]]:
        stored = {(m.platform, m.external_message_id) for m in self.messages}
        return {key for key in keys if key in stored}

    async def bulk_upsert(self, messages: Sequence[NewMessage]) -> list[uuid.UUID]:
        written: list[uuid.UUID] = []
        stored = {(m.platform, m.external_message_id) for m in self.messages}
        for new in messages:
            self.upserted.append(new)
            if new.key in stored:
                continue  # mirrors ON CONFLICT DO NOTHING
            stored.add(new.key)
            entity = Message(
                id=uuid.uuid4(),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                **new.model_dump(mode="python"),
            )
            self.messages.append(entity)
            written.append(entity.id)
        return written

    async def latest_for_users(
        self, user_ids: Sequence[uuid.UUID], *, per_user_limit: int = 25
    ) -> dict[uuid.UUID, list[Message]]:
        grouped: dict[uuid.UUID, list[Message]] = {user_id: [] for user_id in user_ids}
        for message in sorted(self.messages, key=lambda m: m.sent_at, reverse=True):
            bucket = grouped.get(message.sender_user_id)
            if bucket is not None and len(bucket) < per_user_limit:
                bucket.append(message)
        return grouped

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        return len([m for m in self.messages if m.sender_user_id == user_id])

    def _filtered(self, filters: MessageFilters) -> list[Message]:
        """Mirrors the SQL filters, including the substring match being case-insensitive."""
        found = self.messages
        if filters.user_id is not None:
            found = [m for m in found if m.sender_user_id == filters.user_id]
        if filters.platform is not None:
            found = [m for m in found if m.platform == filters.platform]
        if filters.category is not None:
            found = [m for m in found if m.filter_category == filters.category]
        if filters.sent_from is not None:
            found = [m for m in found if m.sent_at >= filters.sent_from]
        if filters.sent_to is not None:
            found = [m for m in found if m.sent_at <= filters.sent_to]
        if filters.search:
            needle = filters.search.strip().lower()
            found = [m for m in found if needle in (m.content or "").lower()]
        return found

    async def search(self, filters: MessageFilters, params: PageParams) -> Paginated[Message]:
        matching = sorted(
            self._filtered(filters), key=lambda m: (m.sent_at, str(m.id)), reverse=True
        )
        window = matching[params.offset : params.offset + params.limit]
        return Paginated(items=window, total=len(matching), params=params)

    async def count(self, filters: MessageFilters) -> int:
        return len(self._filtered(filters))

    async def list_for_user(self, user_id: uuid.UUID) -> list[Message]:
        return sorted(
            [m for m in self.messages if m.sender_user_id == user_id],
            key=lambda m: m.sent_at,
            reverse=True,
        )

    async def counts_by_user(
        self, user_ids: Sequence[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, int]:
        wanted = None if user_ids is None else set(user_ids)
        counts: dict[uuid.UUID, int] = {}
        for message in self.messages:
            if message.sender_user_id is None:
                continue
            if wanted is not None and message.sender_user_id not in wanted:
                continue
            counts[message.sender_user_id] = counts.get(message.sender_user_id, 0) + 1
        return counts

    async def last_sent_by_user(
        self, user_ids: Sequence[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, datetime]:
        wanted = None if user_ids is None else set(user_ids)
        latest: dict[uuid.UUID, datetime] = {}
        for message in self.messages:
            key = message.sender_user_id
            if key is None or (wanted is not None and key not in wanted):
                continue
            if key not in latest or message.sent_at > latest[key]:
                latest[key] = message.sent_at
        return latest

    async def counts_by_platform(self) -> dict[Platform, int]:
        counts: dict[Platform, int] = {}
        for message in self.messages:
            counts[message.platform] = counts.get(message.platform, 0) + 1
        return counts

    async def last_sent_by_platform(self) -> dict[Platform, datetime]:
        latest: dict[Platform, datetime] = {}
        for message in self.messages:
            key = message.platform
            if key not in latest or message.sent_at > latest[key]:
                latest[key] = message.sent_at
        return latest

    async def counts_by_relation(self) -> dict[uuid.UUID, int]:
        counts: dict[uuid.UUID, int] = {}
        for message in self.messages:
            if message.sender_relation_id is not None:
                key = message.sender_relation_id
                counts[key] = counts.get(key, 0) + 1
        return counts

    async def last_sent_by_relation(self) -> dict[uuid.UUID, datetime]:
        latest: dict[uuid.UUID, datetime] = {}
        for message in self.messages:
            key = message.sender_relation_id
            if key is not None and (key not in latest or message.sent_at > latest[key]):
                latest[key] = message.sent_at
        return latest

    async def reattribute(self, relation_id: uuid.UUID, user_id: uuid.UUID | None) -> int:
        moved = 0
        for message in self.messages:
            if message.sender_relation_id == relation_id:
                message.sender_user_id = user_id
                moved += 1
        return moved

    async def delete_for_relation(self, relation_id: uuid.UUID) -> int:
        before = len(self.messages)
        self.messages[:] = [m for m in self.messages if m.sender_relation_id != relation_id]
        return before - len(self.messages)

    async def delete_for_user(self, user_id: uuid.UUID) -> int:
        before = len(self.messages)
        self.messages[:] = [m for m in self.messages if m.sender_user_id != user_id]
        return before - len(self.messages)


class FakeOrgNodeRepository:
    """In-memory stand-in for `OrgNodeRepository` - a plain list of nodes."""

    def __init__(self, nodes: list[OrgNode]) -> None:
        self.nodes = nodes

    async def list_all(self) -> list[OrgNode]:
        return sorted(self.nodes, key=lambda n: (n.position, str(n.id)))

    async def get(self, node_id: uuid.UUID) -> OrgNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    async def children_of(self, node_id: uuid.UUID | None) -> list[OrgNode]:
        matching = [n for n in self.nodes if n.parent_id == node_id]
        return sorted(matching, key=lambda n: (n.position, str(n.id)))

    async def next_position(self, parent_id: uuid.UUID | None) -> int:
        return sum(1 for n in self.nodes if n.parent_id == parent_id)

    async def reindex(self, ordered_ids: Sequence[uuid.UUID]) -> None:
        by_id = {n.id: n for n in self.nodes}
        for index, node_id in enumerate(ordered_ids):
            by_id[node_id].position = index

    async def add(self, node: OrgNode) -> OrgNode:
        if node.id is None:
            node.id = uuid.uuid4()
        if getattr(node, "created_at", None) is None:
            node.created_at = datetime.now(UTC)
        self.nodes.append(node)
        return node

    async def remove(self, node: OrgNode) -> None:
        self.nodes.remove(node)


class FakeOrgNodeMemberRepository:
    """Enforces the unique constraint the real table carries.

    A fake that is more permissive than the database hides exactly the bug it
    should catch: a service that forgets a person can only be in one department
    would pass here and fail in production.
    """

    def __init__(self, memberships: list[OrgNodeMember]) -> None:
        self.memberships = memberships

    async def list_all(self) -> list[OrgNodeMember]:
        return list(self.memberships)

    async def for_node(self, node_id: uuid.UUID) -> list[OrgNodeMember]:
        return [m for m in self.memberships if m.org_node_id == node_id]

    async def for_users(self, user_ids: Sequence[uuid.UUID]) -> list[OrgNodeMember]:
        wanted = set(user_ids)
        return [m for m in self.memberships if m.user_id in wanted]

    async def for_user(self, user_id: uuid.UUID) -> OrgNodeMember | None:
        return next((m for m in self.memberships if m.user_id == user_id), None)

    async def add(self, membership: OrgNodeMember) -> OrgNodeMember:
        if any(m.user_id == membership.user_id for m in self.memberships):
            raise AssertionError(
                "uq_org_node_members_user_id would reject this: the person is "
                "already filed into a department."
            )
        if membership.id is None:
            membership.id = uuid.uuid4()
        self.memberships.append(membership)
        return membership

    async def remove_for_user(self, user_id: uuid.UUID) -> int:
        before = len(self.memberships)
        self.memberships[:] = [m for m in self.memberships if m.user_id != user_id]
        return before - len(self.memberships)


class FakePersonNoteRepository:
    """In-memory stand-in for `PersonNoteRepository`."""

    def __init__(self, notes: list[PersonNote]) -> None:
        self.notes = notes

    async def list_for_user(self, user_id: uuid.UUID) -> list[PersonNote]:
        return sorted(
            [n for n in self.notes if n.user_id == user_id],
            key=lambda n: n.created_at,
            reverse=True,
        )

    async def get(self, note_id: uuid.UUID) -> PersonNote | None:
        return next((n for n in self.notes if n.id == note_id), None)

    async def add(self, note: PersonNote) -> PersonNote:
        if note.id is None:
            note.id = uuid.uuid4()
        if getattr(note, "created_at", None) is None:
            note.created_at = datetime.now(UTC)
        self.notes.append(note)
        return note

    async def remove(self, note: PersonNote) -> None:
        self.notes.remove(note)


class FakeIngestionRunRepository:
    """In-memory stand-in for `IngestionRunRepository`.

    Enforces the `run_id` uniqueness the real table carries, because that
    constraint is the entire point of the upsert: a fake that happily appended a
    second row for the same run would pass the tests that exist to prove it
    cannot happen.
    """

    def __init__(self, runs: list[IngestionRun]) -> None:
        self.runs = runs
        self.decisions: dict[uuid.UUID, list[dict]] = {}

    async def add(self, run: IngestionRun) -> IngestionRun:
        if run.id is None:
            run.id = uuid.uuid4()
        self.runs.append(run)
        return run

    async def upsert_by_run_id(self, values) -> uuid.UUID:  # type: ignore[no-untyped-def]
        """Mirrors `INSERT ... ON CONFLICT (run_id) DO UPDATE`."""
        run_id = values["run_id"]
        existing = next((r for r in self.runs if r.run_id == run_id), None)
        if existing is not None:
            for key, value in values.items():
                if key != "run_id":
                    setattr(existing, key, value)
            return existing.id
        run = IngestionRun(id=uuid.uuid4(), **values)
        self.runs.append(run)
        return run.id

    async def replace_decisions(self, run_pk: uuid.UUID, decisions) -> int:  # type: ignore[no-untyped-def]
        """Mirrors the delete-then-insert the real repository performs."""
        self.decisions[run_pk] = list(decisions)
        return len(self.decisions[run_pk])

    async def get_by_run_id(self, run_id: uuid.UUID) -> IngestionRun | None:
        return next((r for r in self.runs if r.run_id == run_id), None)

    async def list_recent(
        self, *, limit: int = 20, platform: Platform | None = None
    ) -> list[IngestionRun]:
        matching = (
            self.runs if platform is None else [r for r in self.runs if r.platform == platform]
        )
        return sorted(matching, key=lambda r: r.started_at, reverse=True)[:limit]

    async def get(self, run_id: uuid.UUID) -> IngestionRun | None:
        return next((r for r in self.runs if r.id == run_id), None)


class FakeUnitOfWork:
    """Duck-types `app.domains.uow.UnitOfWork`."""

    def __init__(
        self,
        users: list[User] | None = None,
        relations: list[UserRelation] | None = None,
        messages: list[Message] | None = None,
        org_nodes: list[OrgNode] | None = None,
        org_members: list[OrgNodeMember] | None = None,
        notes: list[PersonNote] | None = None,
        runs: list[IngestionRun] | None = None,
    ) -> None:
        self.tenant = TenantContext.global_scope()
        self.users = FakeUserRepository(
            users if users is not None else [], cascade=self._cascade_delete
        )
        self.relations = FakeUserRelationRepository(relations if relations is not None else [])
        self.messages = FakeMessageRepository(messages if messages is not None else [])
        self.org_nodes = FakeOrgNodeRepository(org_nodes if org_nodes is not None else [])
        self.org_members = FakeOrgNodeMemberRepository(
            org_members if org_members is not None else []
        )
        self.notes = FakePersonNoteRepository(notes if notes is not None else [])
        self.runs = FakeIngestionRunRepository(runs if runs is not None else [])
        self.commits = 0
        self.rollbacks = 0
        self.transactions = 0
        #: Counted separately from `commits` so a test can assert that a service
        #: released its connection *before* the slow phase, not merely that it
        #: committed at some point.
        self.checkpoints = 0

    def _cascade_delete(self, user_id: uuid.UUID) -> None:
        """What the database's ON DELETE CASCADE would do."""
        self.relations.relations[:] = [r for r in self.relations.relations if r.user_id != user_id]
        self.messages.messages[:] = [
            m for m in self.messages.messages if m.sender_user_id != user_id
        ]
        self.notes.notes[:] = [n for n in self.notes.notes if n.user_id != user_id]
        self.org_members.memberships[:] = [
            m for m in self.org_members.memberships if m.user_id != user_id
        ]

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        self.transactions += 1
        try:
            yield self
        except Exception:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def checkpoint(self) -> None:
        """Mirrors `SessionUnitOfWork.checkpoint`: a commit that releases the
        connection. Recorded distinctly so tests can assert *when* it happened.
        """
        self.checkpoints += 1
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
