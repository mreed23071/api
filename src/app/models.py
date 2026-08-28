"""Single import surface for the ORM metadata.

Alembic autogenerate only sees a table if the module defining it has been
imported. Every context registers its models here so `Base.metadata` is always
complete - for migrations, and for schema creation in integration tests.

Adding a bounded context with tables means adding one import here. A contract
test asserts that every module named `domains/*/models.py` is represented.
"""

from __future__ import annotations

from app.core.db.base import Base
from app.domains.identity.models import PersonNote, Platform, User, UserRelation
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from app.domains.messaging.models import Message
from app.domains.organization.models import OrgNode, OrgNodeMember

__all__ = [
    "Base",
    "IngestionRun",
    "IngestionRunDecision",
    "Message",
    "OrgNode",
    "OrgNodeMember",
    "PersonNote",
    "Platform",
    "User",
    "UserRelation",
]
