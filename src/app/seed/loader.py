"""Load the console's fixture dataset into a real database.

Two problems have to be solved to turn a JSON file written for a browser into
rows in Postgres, and both answers are worth understanding before reading the
code.

**Identifiers.** The fixtures use readable string ids - `usr_0001`, `acc_1_1`,
`org_root` - while every table here has a UUID primary key. Rather than
inventing new ids and rewriting every cross-reference, each string is hashed
into a UUID with `uuid.uuid5`, which is deterministic: the same input always
produces the same UUID. So `usr_0001` becomes the same id on every machine and
every re-seed, a message's `sender_user_id` maps to the same value its person
did, and the whole reference graph survives the translation intact.

**Idempotency.** Because the ids are deterministic, the loader can ask what is
already there and insert only the rest. Running it twice is safe, running it
against a half-seeded database completes it, and nothing is ever duplicated.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.identity.models import PersonNote, Platform, User, UserRelation
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from app.domains.messaging.models import Message
from app.domains.organization.models import OrgNode, OrgNodeMember

logger = logging.getLogger(__name__)

FIXTURES_PATH = Path(__file__).with_name("fixtures.json")

#: A fixed namespace, so `usr_0001` hashes to the same UUID everywhere and
#: forever. Changing this value orphans every previously seeded row, which is
#: why it is a constant and not derived from anything.
SEED_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def stable_id(kind: str, external: str) -> uuid.UUID:
    """Turn a fixture's string id into a UUID, deterministically.

    `kind` keeps the namespaces apart, so a person and an account that happened
    to share a string id could never collide.
    """
    return uuid.uuid5(SEED_NAMESPACE, f"{kind}:{external}")


def _dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, including the trailing `Z` JavaScript emits."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _date(value: str | None) -> date | None:
    """Parse a plain ISO date."""
    return date.fromisoformat(value) if value else None


@dataclass(slots=True)
class SeedReport:
    """How many rows of each kind the run inserted.

    Zero everywhere means the database was already seeded, which is a success
    rather than a no-op worth worrying about.
    """

    users: int = 0
    relations: int = 0
    messages: int = 0
    notes: int = 0
    org_nodes: int = 0
    memberships: int = 0
    runs: int = 0
    decisions: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            self.users
            + self.relations
            + self.messages
            + self.notes
            + self.org_nodes
            + self.memberships
            + self.runs
            + self.decisions
        )


def load_fixtures(path: Path | None = None) -> dict[str, Any]:
    """Read the fixture file. Separate from seeding so tests can inspect it."""
    # json.loads is typed to return Any - it's the fixture file's own
    # structure, not something worth re-validating field by field here, that
    # makes this dict[str, Any] rather than a guess.
    return cast(dict[str, Any], json.loads((path or FIXTURES_PATH).read_text(encoding="utf-8")))


async def _existing(session: AsyncSession, model: type[Any]) -> set[uuid.UUID]:
    """Which primary keys of one table are already present.

    One query per table rather than a lookup per row. The fixture set is small,
    but the pattern matters: this is the difference between seeding in eight
    queries and seeding in a few thousand.
    """
    rows = await session.execute(select(model.id))
    return set(rows.scalars().all())


def _order_nodes(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort departments so a parent is always inserted before its children.

    The fixtures happen to be in that order already, but relying on that would
    make the loader break silently the first time somebody reorders the file.
    Roots first, then anything whose parent has been placed, repeatedly.
    """
    remaining = list(nodes)
    placed: set[str] = set()
    ordered: list[dict[str, Any]] = []

    while remaining:
        ready = [n for n in remaining if not n["parent_id"] or n["parent_id"] in placed]
        if not ready:
            # A cycle, or a parent that is not in the file at all. Append the
            # rest and let the foreign key be the one to complain, with a
            # message that names the actual problem.
            logger.warning(
                "org fixtures contain nodes whose parents are missing or circular: %s",
                [n["id"] for n in remaining],
            )
            ordered.extend(remaining)
            break
        for node in ready:
            ordered.append(node)
            placed.add(node["id"])
        remaining = [n for n in remaining if n["id"] not in placed]

    return ordered


async def seed_database(
    session: AsyncSession, *, fixtures: dict[str, Any] | None = None
) -> SeedReport:
    """Insert every fixture row that is not already present.

    The caller owns the transaction, so a failure part-way leaves nothing
    behind. Insertion order follows the foreign keys: people, then the accounts
    that point at them, then the messages that point at both.
    """
    data = fixtures if fixtures is not None else load_fixtures()
    report = SeedReport()

    # -- people ------------------------------------------------------------
    known_users = await _existing(session, User)
    for row in data.get("people", []):
        user_id = stable_id("user", row["id"])
        if user_id in known_users:
            continue
        session.add(
            User(
                id=user_id,
                email=row["email"],
                full_name=row["full_name"],
                display_name=row.get("display_name"),
                job_title=row.get("job_title") or None,
                address=row.get("address") or None,
                employment_start=_date(row.get("employment_start")),
                employment_end=_date(row.get("employment_end")),
                timezone=row.get("timezone") or None,
                is_active=row.get("is_active", True),
                created_at=_dt(row.get("created_at")),
                updated_at=_dt(row.get("updated_at")),
            )
        )
        report.users += 1
    await session.flush()

    # -- external accounts -------------------------------------------------
    known_relations = await _existing(session, UserRelation)
    for row in data.get("connected_accounts", []):
        relation_id = stable_id("relation", row["id"])
        if relation_id in known_relations:
            continue
        session.add(
            UserRelation(
                id=relation_id,
                # Null here is the unlinked account - an identity nobody has
                # been attributed to yet. The fixtures include five of them on
                # purpose, so the console's attribution screen has something to
                # work with.
                user_id=stable_id("user", row["user_id"]) if row.get("user_id") else None,
                platform=Platform(row["platform"]),
                external_id=row["external_id"],
                external_handle=row.get("external_handle"),
                external_email=row.get("external_email") or None,
                is_primary=row.get("is_primary", False),
                details={},
                created_at=_dt(row.get("created_at")),
            )
        )
        report.relations += 1
    await session.flush()

    # -- messages ----------------------------------------------------------
    known_messages = await _existing(session, Message)
    for row in data.get("messages", []):
        message_id = stable_id("message", row["id"])
        if message_id in known_messages:
            continue
        # Commit detail is one connector's shape, so it rides in the metadata
        # blob rather than in columns. `MessageRead` lifts it back out.
        metadata: dict[str, Any] = {}
        if row.get("commit"):
            metadata["commit"] = row["commit"]
        session.add(
            Message(
                id=message_id,
                kind=row.get("kind", "message"),
                sender_user_id=(
                    stable_id("user", row["sender_user_id"]) if row.get("sender_user_id") else None
                ),
                sender_relation_id=(
                    stable_id("relation", row["sender_relation_id"])
                    if row.get("sender_relation_id")
                    else None
                ),
                platform=Platform(row["platform"]),
                external_message_id=row["external_message_id"],
                conversation_id=row.get("conversation_id"),
                content=row["content"],
                # Deliberately unembedded. Vectors are produced by a model this
                # loader has no business starting, and every column that needs
                # one is nullable.
                embedding=None,
                embedding_model=row.get("embedding_model"),
                filter_category=row.get("filter_category"),
                filter_reason=row.get("filter_reason"),
                filter_prompt_version="fixture",
                sent_at=_dt(row["sent_at"]),
                source_metadata=metadata,
            )
        )
        report.messages += 1
    await session.flush()

    # -- notes -------------------------------------------------------------
    known_notes = await _existing(session, PersonNote)
    for row in data.get("person_notes", []):
        note_id = stable_id("note", row["id"])
        if note_id in known_notes:
            continue
        session.add(
            PersonNote(
                id=note_id,
                user_id=stable_id("user", row["user_id"]),
                author=row["author"],
                body=row["body"],
                created_at=_dt(row.get("created_at")),
            )
        )
        report.notes += 1
    await session.flush()

    # -- departments and membership ----------------------------------------
    known_nodes = await _existing(session, OrgNode)
    # Seeds `position` per parent from whatever already exists, so a fixture
    # added to a *half*-seeded database (some nodes already present) still
    # appends after them instead of colliding on the same position.
    sibling_count: dict[uuid.UUID | None, int] = Counter(
        row[0] for row in (await session.execute(select(OrgNode.parent_id))).all()
    )
    known_members = {
        m.user_id for m in (await session.execute(select(OrgNodeMember))).scalars().all()
    }
    for row in _order_nodes(data.get("org_nodes", [])):
        node_id = stable_id("org_node", row["id"])
        if node_id not in known_nodes:
            parent_id = stable_id("org_node", row["parent_id"]) if row.get("parent_id") else None
            session.add(
                OrgNode(
                    id=node_id,
                    name=row["name"],
                    subtitle=row.get("subtitle") or None,
                    parent_id=parent_id,
                    position=sibling_count[parent_id],
                    created_at=_dt(row.get("created_at")),
                )
            )
            sibling_count[parent_id] += 1
            report.org_nodes += 1
        await session.flush()

        for member in row.get("member_ids", []):
            member_id = stable_id("user", member)
            # One department per person is a database constraint, so a person
            # already filed anywhere is skipped rather than allowed to fail the
            # whole seed.
            if member_id in known_members:
                continue
            session.add(OrgNodeMember(org_node_id=node_id, user_id=member_id))
            known_members.add(member_id)
            report.memberships += 1
    await session.flush()

    # -- ingestion history --------------------------------------------------
    known_runs = await _existing(session, IngestionRun)
    for row in data.get("ingestion_runs", []):
        run_id = stable_id("run", row["run_id"])
        if run_id in known_runs:
            continue
        run = IngestionRun(
            id=run_id,
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row.get("finished_at")),
            duration_ms=row.get("duration_ms", 0),
            dry_run=row.get("dry_run", False),
            fetched=row.get("fetched", 0),
            already_ingested=row.get("already_ingested", 0),
            evaluated=row.get("evaluated", 0),
            retained=row.get("retained", 0),
            discarded=row.get("discarded", 0),
            embedded=row.get("embedded", 0),
            persisted=row.get("persisted", 0),
            users_provisioned=row.get("users_provisioned", 0),
            filter_errors=row.get("filter_errors", 0),
            filter_provider=row.get("filter_provider"),
            embedding_model=row.get("embedding_model"),
            status=row.get("status", "success"),
        )
        # One verdict per message per run - `uq_run_decision_message` enforces
        # it, and two runs in the shipped dataset carry the same message twice.
        # Keeping the first occurrence matches how migration 0007 deduplicates
        # the same shape in an existing database (earliest wins), so a seeded
        # database and a migrated one end up describing a run identically.
        seen_in_run: set[str] = set()
        run.decisions = []
        for decision in row.get("decisions", []):
            external_message_id = decision["id"]
            if external_message_id in seen_in_run:
                continue
            seen_in_run.add(external_message_id)
            run.decisions.append(
                IngestionRunDecision(
                    external_message_id=external_message_id,
                    keep=decision["keep"],
                    category=decision.get("category"),
                    reason=decision.get("reason"),
                    is_fallback=decision.get("is_fallback", False),
                )
            )
        report.decisions += len(run.decisions)
        session.add(run)
        report.runs += 1
    await session.flush()

    return report


def summarise(report: SeedReport) -> str:
    """One line per table, for the command line."""
    lines: Iterable[str] = (
        f"  {label:<14} {count}"
        for label, count in (
            ("people", report.users),
            ("accounts", report.relations),
            ("messages", report.messages),
            ("notes", report.notes),
            ("departments", report.org_nodes),
            ("memberships", report.memberships),
            ("runs", report.runs),
            ("decisions", report.decisions),
        )
    )
    header = "Nothing to do - already seeded." if report.total == 0 else "Inserted:"
    return "\n".join([header, *lines])
