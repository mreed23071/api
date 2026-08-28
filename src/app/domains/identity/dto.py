"""Internal contracts published by the identity context.

Not wire types. The API layer maps these to versioned schemas; a change here is
an internal refactor, a change to `app.api.v1.schemas` is a breaking API change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from app.domains.identity.models import Platform, User, UserRelation


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    """A third-party identity observed by some connector.

    Deliberately knows nothing about messages: the identity context resolves
    *people*, and the ingestion context is the only place that knows a person
    was observed by way of a message.
    """

    platform: Platform
    external_id: str
    handle: str | None = None
    email: str | None = None
    display_name: str | None = None

    @property
    def key(self) -> tuple[Platform, str]:
        """The pair that identifies this external identity uniquely.

        Matches the `(platform, external_id)` unique constraint: one Slack
        account belongs to exactly one internal person.
        """
        return (self.platform, self.external_id)


@dataclass(slots=True)
class IdentityResolution:
    """Outcome of resolving a batch of candidates."""

    relations: dict[tuple[Platform, str], UserRelation] = field(default_factory=dict)
    users_created: int = 0
    relations_created: int = 0

    def __getitem__(self, key: tuple[Platform, str]) -> UserRelation:
        """Look a resolved relation up by its key, as `resolution[key]`.

        Defining `__getitem__` is what makes square-bracket indexing work on a
        custom object in Python. It raises `KeyError` for an unresolved
        identity, which is correct here: every candidate passed in was resolved,
        so a miss means the caller is asking about something it never submitted.
        """
        return self.relations[key]


# ---------------------------------------------------------------------------
# The directory: people, their accounts, and the notes kept about them.
#
# These are the contracts the console-facing service speaks. They are separate
# from the resolution contracts above because they answer a different question:
# those describe an identity a connector observed, these describe a person
# somebody is administering.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewPerson:
    """A person created by hand rather than discovered by a connector."""

    full_name: str
    email: str
    display_name: str | None = None
    job_title: str | None = None
    address: str | None = None
    timezone: str | None = None
    employment_start: date | None = None


@dataclass(frozen=True, slots=True)
class PersonPatch:
    """A partial update. `None` means "leave alone" for every field here.

    Nothing on a person is meaningfully set *to* null by an edit, so unlike the
    organization patch this needs no companion flag - clearing a field is done
    by sending an empty string.
    """

    full_name: str | None = None
    display_name: str | None = None
    job_title: str | None = None
    address: str | None = None
    timezone: str | None = None
    employment_start: date | None = None
    employment_end: date | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class PersonView:
    """A person with the counts the directory list shows beside them.

    Assembled from three contexts - the person, their accounts, their messages,
    and their department - which is why it is a view rather than an entity.
    """

    user: User
    platforms: list[Platform] = field(default_factory=list)
    message_count: int = 0
    last_message_at: datetime | None = None
    department_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class NewAccount:
    """An external account attached to a person by hand."""

    user_id: uuid.UUID
    platform: Platform
    external_handle: str
    external_email: str | None = None


@dataclass(frozen=True, slots=True)
class UnlinkedAccountView:
    """An account nobody has claimed, with enough context to claim it.

    The counts are the whole point: deciding who an unattributed Slack handle
    belongs to is guesswork without knowing how much it has said and when it
    last said anything.
    """

    relation: UserRelation
    message_count: int
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class ForgetResult:
    """What erasure removed.

    Reported back rather than swallowed, because "forgotten" and "forgotten,
    along with two hundred messages" are different facts, and the second one is
    the one worth recording.
    """

    deleted_messages: int
    deleted_accounts: int


@dataclass(frozen=True, slots=True)
class AccountDeletionResult:
    """What deleting an account destroyed, so the caller can report it."""

    deleted_messages: int
