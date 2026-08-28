"""The one place the console surface's access rule lives - deliberately.

The routes that mirror the console are unauthenticated on purpose while the
platforms are being wired together. That is a real decision with a real risk:
the usual way it goes wrong is that "temporarily open" becomes permanent because
nobody can find all the places it was decided.

So it is decided once, here. Every service method on the console surface calls
`require_console_access`, and closing that surface is a change to this function -
not a hunt through thirty call sites. When the roles and permissions work lands,
the body becomes a `can(...)` check and the call sites do not move.
"""

from __future__ import annotations

from app.core.security.principal import Principal, Scope

#: Flip to True to require the admin scope on every console route, which is the
#: cheap stopgap if this surface is ever exposed before the RBAC work lands.
#: The real replacement is a permission check per operation - see the
#: authorization proposal - and this flag exists so that "close it now" is
#: always one line away.
ENFORCED = False


def require_console_access(principal: Principal) -> None:
    """Assert the caller may use the console surface.

    A no-op today. Kept as a call rather than an absence so that the surface has
    a single, greppable enforcement point from the first commit: an
    authorization check that has to be *added* everywhere later is the one that
    gets missed somewhere.
    """
    if ENFORCED:
        principal.require(Scope.ADMIN)
