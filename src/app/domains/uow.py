"""The application's unit of work.

Lives in the domain layer, not in `core`, because it is the one object that
knows which repositories exist. `core` must never import a bounded context; this
module is where those two facts are reconciled.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.uow import SessionUnitOfWork
from app.core.security.principal import TenantContext
from app.domains.identity.repository import (
    PersonNoteRepository,
    UserRelationRepository,
    UserRepository,
)
from app.domains.ingestion.repository import IngestionRunRepository
from app.domains.messaging.repository import MessageRepository
from app.domains.organization.repository import (
    OrgNodeMemberRepository,
    OrgNodeRepository,
)


class UnitOfWork(SessionUnitOfWork):
    """One session, one tenant, every repository.

    Services take a `UnitOfWork` rather than a session, which is what stops a
    repository being constructed ad hoc somewhere without a tenant.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        super().__init__(session)
        self.tenant = tenant
        self.users = UserRepository(session, tenant)
        self.relations = UserRelationRepository(session, tenant)
        self.notes = PersonNoteRepository(session, tenant)
        self.messages = MessageRepository(session, tenant)
        self.org_nodes = OrgNodeRepository(session, tenant)
        self.org_members = OrgNodeMemberRepository(session, tenant)
        self.runs = IngestionRunRepository(session, tenant)
