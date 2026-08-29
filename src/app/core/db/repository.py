"""Repository base class - and the one place tenant isolation is applied."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security.principal import TenantContext


class Repository:
    """Common surface for every repository.

    Repositories are constructed per unit of work, never per process: they hold
    a session and the tenant the current principal is acting within.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    # -- the tenancy seam --------------------------------------------------
    #
    # Today mabinsoft is single-tenant: `scoped()` is the identity function and
    # every principal carries `TenantContext.global_scope()`. The seam exists so
    # that adding multi-tenancy is a *mechanical* change rather than an
    # architectural one:
    #
    #   1. add `organization_id` to the tables (one migration),
    #   2. give the models a `TenantScopedMixin`,
    #   3. implement the body of this method once,
    #   4. every existing query becomes tenant-safe, because they all route
    #      through here.
    #
    # The rule that makes step 4 true: **a repository must never build a SELECT
    # that does not pass through `scoped()`.** `tests/contract/test_layering.py`
    # asserts it.

    def scoped(self, statement: Select[Any], model: type[Any] | None = None) -> Select[Any]:
        """Constrain a statement to the current principal's tenant."""
        if self.tenant.is_global:
            return statement
        # When organization_id exists:
        #     column = getattr(model or _entity_of(statement), "organization_id")
        #     return statement.where(column == self.tenant.tenant_id)
        raise NotImplementedError(
            "Tenant-scoped queries are not implemented yet. A non-global "
            "TenantContext reached the repository layer, which means multi-tenancy "
            "was switched on without implementing Repository.scoped()."
        )
