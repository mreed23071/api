"""Data access for the identity context.

Every SELECT passes through `self.scoped(...)`, which is the single hook where
tenant isolation will be applied. `tests/contract/test_layering.py` enforces it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from app.core.db.repository import Repository
from app.core.pagination import PageParams, Paginated
from app.domains.identity.models import PersonNote, Platform, User, UserRelation


class UserRepository(Repository):
    """Reads and writes rows in the `users` table."""

    def _select(self, *, active_only: bool) -> Select[tuple[User]]:
        """Build the base query every read in this class starts from.

        Factored out so the scoping hook and the active-only filter are applied
        identically everywhere, including by `count`, which must count exactly
        the rows `list_users` would return.
        """
        statement = self.scoped(select(User), User)
        if active_only:
            statement = statement.where(User.is_active.is_(True))
        return statement

    async def list_users(
        self,
        params: PageParams,
        *,
        active_only: bool = True,
        with_relations: bool = False,
    ) -> Paginated[User]:
        """One page of people, plus the total, for a paginated response.

        `with_relations` uses `selectinload` rather than a join: one extra round
        trip that returns each user once, instead of one query that returns a
        user's columns repeated for every account they hold.
        """
        statement = (
            self._select(active_only=active_only)
            .order_by(User.created_at.asc(), User.id.asc())
            .limit(params.limit)
            .offset(params.offset)
        )
        if with_relations:
            # selectinload, not joinedload: one extra round trip beats a
            # cartesian product, and lazy="raise" forbids implicit loading.
            statement = statement.options(selectinload(User.relations))

        items = (await self.session.execute(statement)).scalars().all()
        total = await self.count(active_only=active_only)
        return Paginated(items=items, total=total, params=params)

    async def count(self, *, active_only: bool = True) -> int:
        """How many people match, counted in the database rather than loaded."""
        statement = select(func.count()).select_from(
            self._select(active_only=active_only).subquery()
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def list_all(self, *, active_only: bool = False) -> Sequence[User]:
        """Everyone, unpaged.

        The directory shows the whole roster; paging it would mean the console
        cannot compute "how many people are in this department" without asking
        again. Bounded by headcount, which is the one table here that is.
        """
        statement = self._select(active_only=active_only).order_by(
            User.full_name.asc(), User.id.asc()
        )
        return (await self.session.execute(statement)).scalars().all()

    async def get(self, user_id: uuid.UUID) -> User | None:
        """Fetch one person by id, or `None`."""
        statement = self.scoped(select(User), User).where(User.id == user_id)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """Fetch one person by email - the key identity resolution merges on."""
        statement = self.scoped(select(User), User).where(User.email == email)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def add(self, user: User) -> User:
        """Stage a new person and flush, so their generated id is readable."""
        self.session.add(user)
        await self.session.flush()
        return user

    async def remove(self, user: User) -> None:
        """Erasure. Accounts, notes and messages follow by ON DELETE CASCADE."""
        await self.session.delete(user)
        await self.session.flush()


class UserRelationRepository(Repository):
    """Reads and writes rows in `user_relations` - the external accounts."""

    async def resolve(self, platform: Platform, external_id: str) -> UserRelation | None:
        """Find the account matching one platform identity, or `None`."""
        statement = self.scoped(select(UserRelation), UserRelation).where(
            UserRelation.platform == platform,
            UserRelation.external_id == external_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def resolve_many(
        self, identities: Sequence[tuple[Platform, str]]
    ) -> dict[tuple[Platform, str], UserRelation]:
        """Batch variant - one query for a whole ingestion run."""
        if not identities:
            return {}
        conditions = [
            (UserRelation.platform == platform) & (UserRelation.external_id == external_id)
            for platform, external_id in identities
        ]
        clause = conditions[0]
        for condition in conditions[1:]:
            clause = clause | condition

        statement = self.scoped(select(UserRelation), UserRelation).where(clause)
        rows = (await self.session.execute(statement)).scalars().all()
        return {(relation.platform, relation.external_id): relation for relation in rows}

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[UserRelation]:
        """Every external account attributed to one person."""
        statement = self.scoped(select(UserRelation), UserRelation).where(
            UserRelation.user_id == user_id
        )
        return (await self.session.execute(statement)).scalars().all()

    async def get(self, relation_id: uuid.UUID) -> UserRelation | None:
        """Fetch one external account by id, or `None`."""
        statement = self.scoped(select(UserRelation), UserRelation).where(
            UserRelation.id == relation_id
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list_all(self) -> Sequence[UserRelation]:
        """Every external account, linked or not, oldest first."""
        statement = self.scoped(select(UserRelation), UserRelation).order_by(
            UserRelation.created_at.asc()
        )
        return (await self.session.execute(statement)).scalars().all()

    async def for_users(self, user_ids: Sequence[uuid.UUID]) -> Sequence[UserRelation]:
        """Every linked account belonging to any of these people.

        What a paginated directory listing uses instead of `list_all` - its
        query cost tracks the page size rather than every account ever linked.
        """
        if not user_ids:
            return []
        statement = self.scoped(select(UserRelation), UserRelation).where(
            UserRelation.user_id.in_(user_ids)
        )
        return (await self.session.execute(statement)).scalars().all()

    async def list_unlinked(self) -> Sequence[UserRelation]:
        """Accounts nobody has been attributed to yet.

        The reason `user_id` is nullable: ingestion discovers external
        identities, and deciding which person one belongs to is a separate,
        reversible act rather than something the connector can know.
        """
        statement = (
            self.scoped(select(UserRelation), UserRelation)
            .where(UserRelation.user_id.is_(None))
            .order_by(UserRelation.created_at.asc())
        )
        return (await self.session.execute(statement)).scalars().all()

    async def counts_by_platform(self) -> dict[Platform, int]:
        """How many accounts are known on each platform, linked or not."""
        statement = self.scoped(
            select(UserRelation.platform, func.count()), UserRelation
        ).group_by(UserRelation.platform)
        rows = await self.session.execute(statement)
        return {platform: int(count) for platform, count in rows.all()}

    async def has_primary(self, user_id: uuid.UUID) -> bool:
        """Whether this person already has a primary account.

        The first account linked to somebody becomes their primary; later ones
        do not displace it. Asking rather than assuming keeps that rule in one
        place instead of implied by insertion order.
        """
        statement = self.scoped(select(UserRelation.id), UserRelation).where(
            UserRelation.user_id == user_id,
            UserRelation.is_primary.is_(True),
        )
        return (await self.session.execute(statement)).first() is not None

    async def add(self, relation: UserRelation) -> UserRelation:
        """Stage a new external account and flush, so its id is readable."""
        self.session.add(relation)
        await self.session.flush()
        return relation

    async def remove(self, relation: UserRelation) -> None:
        """Delete one external account. Its messages are the caller's problem."""
        await self.session.delete(relation)
        await self.session.flush()


class PersonNoteRepository(Repository):
    """Notes a human wrote about a person.

    Separate from the generated summaries on purpose: those are derived and can
    be rebuilt, these cannot.
    """

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[PersonNote]:
        """Notes about one person, newest first."""
        statement = (
            self.scoped(select(PersonNote), PersonNote)
            .where(PersonNote.user_id == user_id)
            .order_by(PersonNote.created_at.desc())
        )
        return (await self.session.execute(statement)).scalars().all()

    async def get(self, note_id: uuid.UUID) -> PersonNote | None:
        """Fetch one note by id, or `None`."""
        statement = self.scoped(select(PersonNote), PersonNote).where(PersonNote.id == note_id)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def add(self, note: PersonNote) -> PersonNote:
        """Stage a note for insertion and fill in its generated id and timestamp."""
        self.session.add(note)
        await self.session.flush()
        return note

    async def remove(self, note: PersonNote) -> None:
        """Delete one note."""
        await self.session.delete(note)
        await self.session.flush()
