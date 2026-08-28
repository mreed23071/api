"""The directory: people, their accounts, and the notes kept about them.

Separate from `IdentityService`, which exists to resolve identities a connector
observed during ingestion. This service answers a different question - what an
administrator is doing to a person's record - and the two have different
callers, different failure modes and, before long, different permissions.

Reattribution is the operation worth reading twice. Linking an account to a
person moves every message that arrived on it onto that person; unlinking
returns them to the unresolved pool. `sender_relation_id` never moves, so
provenance survives both.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.pagination import PageParams, Paginated
from app.core.security.principal import Principal
from app.core.security.provisional import require_console_access
from app.domains.identity.dto import (
    AccountDeletionResult,
    ForgetResult,
    NewAccount,
    NewPerson,
    PersonPatch,
    PersonView,
    UnlinkedAccountView,
)
from app.domains.identity.models import PersonNote, Platform, User, UserRelation
from app.domains.messaging.models import Message
from app.domains.organization.models import OrgNodeMember
from app.domains.uow import UnitOfWork

logger = logging.getLogger(__name__)

#: A hand-created account has no external id from the platform yet - nobody has
#: authenticated against it. One is synthesised so the (platform, external_id)
#: uniqueness constraint still holds, and so a later connector observation can
#: be recognised as the same account rather than colliding with it.
_SYNTHETIC_ID_LENGTH = 12


class DirectoryService:
    """Console-facing operations over people, accounts and notes."""

    def __init__(self, uow: UnitOfWork, principal: Principal) -> None:
        self.uow = uow
        self.principal = principal

    # -- people -------------------------------------------------------------

    async def list_people(self) -> list[PersonView]:
        """Return everyone, with the counts and badges the directory shows.

        Four queries regardless of how many people there are: the people, their
        accounts, their message counts, and their departments. The alternative
        is one query per person, which is the shape that looks fine with twelve
        rows and falls over at a thousand.

        Unpaged, deliberately - this is what the org chart, sender-name lookups
        and the account-linking picker call, and all three need the complete
        roster rather than a page of it. `list_people_page` is the counterpart
        for the directory *screen*, which can afford to show one page at a time
        and is the one growing without bound.
        """
        require_console_access(self.principal)
        users = await self.uow.users.list_all()
        relations = await self.uow.relations.list_all()
        memberships = await self.uow.org_members.list_all()
        counts = await self.uow.messages.counts_by_user()
        latest = await self.uow.messages.last_sent_by_user()
        return self._assemble_views(users, relations, memberships, counts, latest)

    async def list_people_page(self, page: PageParams) -> Paginated[PersonView]:
        """One page of the directory, plus the total.

        Same four-query shape as `list_people`, but every query is scoped to
        just this page's user ids - the query cost tracks the page size
        instead of the size of the roster, which is what makes this safe to
        call as the roster grows past what `list_people` should still be
        asked to return in one response.
        """
        require_console_access(self.principal)
        result = await self.uow.users.list_users(page, active_only=False)
        user_ids = [user.id for user in result.items]
        relations = await self.uow.relations.for_users(user_ids)
        memberships = await self.uow.org_members.for_users(user_ids)
        counts = await self.uow.messages.counts_by_user(user_ids)
        latest = await self.uow.messages.last_sent_by_user(user_ids)
        views = self._assemble_views(result.items, relations, memberships, counts, latest)
        return Paginated(items=views, total=result.total, params=result.params)

    @staticmethod
    def _assemble_views(
        users: Sequence[User],
        relations: Sequence[UserRelation],
        memberships: Sequence[OrgNodeMember],
        counts: dict[uuid.UUID, int],
        latest: dict[uuid.UUID, datetime],
    ) -> list[PersonView]:
        """Combine the person, their accounts, their message stats and their
        department into the one row the directory shows per person."""
        platforms: dict[uuid.UUID, list[Platform]] = {}
        for relation in relations:
            if relation.user_id is None:
                continue
            seen = platforms.setdefault(relation.user_id, [])
            if relation.platform not in seen:
                seen.append(relation.platform)

        department = {m.user_id: m.org_node_id for m in memberships}

        return [
            PersonView(
                user=user,
                platforms=platforms.get(user.id, []),
                message_count=counts.get(user.id, 0),
                last_message_at=latest.get(user.id),
                department_id=department.get(user.id),
            )
            for user in users
        ]

    async def get_person(self, user_id: uuid.UUID) -> User:
        """Fetch one person, raising `NotFoundError` if there is no such id."""
        require_console_access(self.principal)
        return await self._require_user(user_id)

    async def create_person(self, new: NewPerson) -> User:
        """Create a person by hand, rather than discovering them via a connector.

        Three things happen before the write, and each prevents a specific mess:

        * the email is lowercased, because it is the key used to decide that two
          observed identities are the same human. `Amara@x.com` and
          `amara@x.com` must not become two people.
        * name and email are required. A directory of blank rows is worse than
          an error message.
        * the email is checked for a duplicate, raising `ConflictError` (a 409)
          rather than letting the database's unique index raise something
          opaque.

        `new.display_name or full_name.split(" ")[0] or full_name` is a Python
        idiom worth knowing: `or` returns the first value that is "truthy", so
        this reads as "the supplied display name, else the first word of their
        name, else the whole name". Empty strings are falsy, which is why the
        final fallback is there at all.
        """
        require_console_access(self.principal)
        email = new.email.strip().lower()
        full_name = new.full_name.strip()
        if not email or not full_name:
            raise ValidationError("A person needs a name and an email address.")

        if await self.uow.users.get_by_email(email) is not None:
            raise ConflictError(
                "A person with that email address already exists.",
                details={"email": email},
            )

        async with self.uow.transaction():
            user = await self.uow.users.add(
                User(
                    email=email,
                    full_name=full_name,
                    # The first token of the full name is a better default than
                    # the whole thing: a display name is what a colleague would
                    # call them, and it is trivially editable afterwards.
                    display_name=new.display_name or full_name.split(" ")[0] or full_name,
                    job_title=new.job_title,
                    address=new.address,
                    timezone=new.timezone or "UTC",
                    employment_start=new.employment_start or datetime.now(UTC).date(),
                    is_active=True,
                )
            )
        return user

    async def update_person(self, user_id: uuid.UUID, patch: PersonPatch) -> User:
        """Apply a partial update - only the fields the patch actually names.

        The loop uses `getattr(patch, field_name)` and `setattr(user, ...)`,
        which read and write an attribute by *name at runtime* rather than by
        writing `patch.job_title` eight times. The trade is real: it is shorter
        and cannot drift out of sync with itself, but a typo in the tuple of
        names fails at runtime rather than being caught by the type checker.
        The tuple is the list of fields a caller is allowed to change, so a
        field not named here can never be edited through this route.

        Mutating `user` is enough to save it. It is a live ORM object attached
        to the session, so SQLAlchemy notices the change and emits the UPDATE at
        flush time; there is no explicit "save" call.
        """
        require_console_access(self.principal)
        user = await self._require_user(user_id)

        async with self.uow.transaction():
            for field_name in (
                "full_name",
                "display_name",
                "job_title",
                "address",
                "timezone",
                "employment_start",
                "employment_end",
                "is_active",
            ):
                value = getattr(patch, field_name)
                if value is not None:
                    setattr(user, field_name, value)
            await self.uow.flush()
        return user

    async def forget_person(self, user_id: uuid.UUID) -> ForgetResult:
        """Erasure: the person, their accounts, their notes, their messages.

        Counted before the delete rather than after, because the cascade takes
        the evidence with it. What is returned is the only record that survives
        of how much was removed - which is exactly what an erasure request needs
        to be able to demonstrate afterwards.
        """
        require_console_access(self.principal)
        user = await self._require_user(user_id)

        message_count = await self.uow.messages.count_for_user(user_id)
        accounts = await self.uow.relations.list_for_user(user_id)

        async with self.uow.transaction():
            await self.uow.users.remove(user)

        logger.info(
            "person erased",
            extra={
                "user_id": str(user_id),
                "deleted_messages": message_count,
                "deleted_accounts": len(accounts),
            },
        )
        return ForgetResult(deleted_messages=message_count, deleted_accounts=len(accounts))

    async def list_messages_for(self, user_id: uuid.UUID) -> Sequence[Message]:
        """Every message attributed to one person, newest first.

        Checks the person exists first, so an unknown id gives a 404 rather
        than an empty list that reads as "this person has said nothing".
        """
        require_console_access(self.principal)
        await self._require_user(user_id)
        return await self.uow.messages.list_for_user(user_id)

    # -- accounts -----------------------------------------------------------

    async def list_accounts_for(self, user_id: uuid.UUID) -> Sequence[UserRelation]:
        """List the external accounts attributed to one person."""
        require_console_access(self.principal)
        await self._require_user(user_id)
        return await self.uow.relations.list_for_user(user_id)

    async def list_unlinked_accounts(self) -> list[UnlinkedAccountView]:
        """List accounts nobody has been attributed to, with the context to decide.

        Three queries: the unattributed accounts, how many messages each has
        sent, and when each last spoke. The counts are the entire value of this
        screen - deciding whose Slack handle `@a.okafor` is remains guesswork
        without knowing it has sent four hundred messages, most recently
        yesterday.

        `counts.get(relation.id, 0)` returns 0 when the account has said nothing
        at all, rather than raising the way `counts[relation.id]` would.
        """
        require_console_access(self.principal)
        unlinked = await self.uow.relations.list_unlinked()
        counts = await self.uow.messages.counts_by_relation()
        latest = await self.uow.messages.last_sent_by_relation()
        return [
            UnlinkedAccountView(
                relation=relation,
                message_count=counts.get(relation.id, 0),
                last_seen_at=latest.get(relation.id),
            )
            for relation in unlinked
        ]

    async def create_account(self, new: NewAccount) -> UserRelation:
        """Attach an external account to a person by hand.

        `is_primary` is set to `not await self.uow.relations.has_primary(...)` -
        that is, this account becomes the primary one only if the person does
        not already have a primary. First one wins; later ones do not displace
        it.
        """
        require_console_access(self.principal)
        await self._require_user(new.user_id)
        handle = new.external_handle.strip()
        if not handle:
            raise ValidationError("An account needs a handle.")

        async with self.uow.transaction():
            relation = await self.uow.relations.add(
                UserRelation(
                    user_id=new.user_id,
                    platform=new.platform,
                    external_id=self._synthetic_external_id(new.platform),
                    external_handle=handle,
                    external_email=new.external_email,
                    is_primary=not await self.uow.relations.has_primary(new.user_id),
                )
            )
        return relation

    async def link_account(self, relation_id: uuid.UUID, user_id: uuid.UUID) -> UserRelation:
        """Attribute an account to a person, and every message it ever sent."""
        require_console_access(self.principal)
        relation = await self._require_relation(relation_id)
        await self._require_user(user_id)

        async with self.uow.transaction():
            relation.user_id = user_id
            relation.is_primary = not await self.uow.relations.has_primary(user_id)
            await self.uow.flush()
            moved = await self.uow.messages.reattribute(relation_id, user_id)

        logger.info(
            "account linked",
            extra={
                "relation_id": str(relation_id),
                "user_id": str(user_id),
                "reattributed_messages": moved,
            },
        )
        return relation

    async def unlink_account(self, relation_id: uuid.UUID) -> UserRelation:
        """Detach an account; its messages return to the unresolved pool."""
        require_console_access(self.principal)
        relation = await self._require_relation(relation_id)
        previous = relation.user_id

        async with self.uow.transaction():
            relation.user_id = None
            # A detached account is nobody's primary. Leaving the flag set would
            # mean the next link silently declines to make itself primary.
            relation.is_primary = False
            await self.uow.flush()
            moved = await self.uow.messages.reattribute(relation_id, None)

        logger.info(
            "account unlinked",
            extra={
                "relation_id": str(relation_id),
                "previous_user_id": str(previous) if previous else None,
                "orphaned_messages": moved,
            },
        )
        return relation

    async def delete_account(self, relation_id: uuid.UUID) -> AccountDeletionResult:
        """Remove an account and everything that arrived on it.

        Distinct from unlinking, and destructive: unlink keeps the messages and
        forgets who sent them, this keeps nothing.
        """
        require_console_access(self.principal)
        relation = await self._require_relation(relation_id)

        async with self.uow.transaction():
            deleted = await self.uow.messages.delete_for_relation(relation_id)
            await self.uow.relations.remove(relation)

        return AccountDeletionResult(deleted_messages=deleted)

    # -- notes --------------------------------------------------------------

    async def list_notes(self, user_id: uuid.UUID) -> Sequence[PersonNote]:
        """List the notes written about a person, newest first."""
        require_console_access(self.principal)
        await self._require_user(user_id)
        return await self.uow.notes.list_for_user(user_id)

    async def add_note(self, user_id: uuid.UUID, body: str, author: str) -> PersonNote:
        """Record an observation about a person.

        `author.strip() or "unknown"` falls back when the author is blank -
        again, `or` returning the first truthy value. Once operators are rows in
        their own right this becomes a foreign key, but a note has to outlive
        the account of whoever wrote it, so the text is kept either way.
        """
        require_console_access(self.principal)
        await self._require_user(user_id)
        text = body.strip()
        if not text:
            raise ValidationError("A note needs something in it.")

        async with self.uow.transaction():
            note = await self.uow.notes.add(
                PersonNote(user_id=user_id, author=author.strip() or "unknown", body=text)
            )
        return note

    async def delete_note(self, note_id: uuid.UUID) -> uuid.UUID:
        """Delete one note and return its id, so the caller can confirm which."""
        require_console_access(self.principal)
        note = await self.uow.notes.get(note_id)
        if note is None:
            raise NotFoundError("Note not found.", details={"note_id": str(note_id)})

        async with self.uow.transaction():
            await self.uow.notes.remove(note)
        return note_id

    # -- internals ----------------------------------------------------------

    async def _require_user(self, user_id: uuid.UUID) -> User:
        """Fetch a person, or raise `NotFoundError`.

        Internal helper - the leading underscore is a convention meaning
        "nothing outside this class should call this". Raising here is what
        lets every method above it assume the person exists.
        """
        user = await self.uow.users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.", details={"user_id": str(user_id)})
        return user

    async def _require_relation(self, relation_id: uuid.UUID) -> UserRelation:
        """Fetch an account, or raise `NotFoundError`."""
        relation = await self.uow.relations.get(relation_id)
        if relation is None:
            raise NotFoundError("Account not found.", details={"account_id": str(relation_id)})
        return relation

    @staticmethod
    def _synthetic_external_id(platform: Platform) -> str:
        """Invent an external id for an account nobody has authenticated against.

        Real accounts arrive from a connector carrying the platform's own id.
        A hand-created one has none, but `(platform, external_id)` is unique in
        the database, so something has to fill the slot. `uuid.uuid4().hex` is a
        random 32-character hex string; the first twelve characters are far more
        than enough to avoid a collision here.
        """
        return f"{platform.value.upper()}-{uuid.uuid4().hex[:_SYNTHETIC_ID_LENGTH]}"
