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

from app.core.config import get_settings
from app.core.security.principal import Principal, Scope


def require_console_access(principal: Principal) -> None:
    """Assert the caller may use the console surface.

    A no-op while `CONSOLE_ACCESS_ENFORCED` is false, which is the default for
    local development. Kept as a call rather than an absence so that the surface
    has a single, greppable enforcement point from the first commit: an
    authorization check that has to be *added* everywhere later is the one that
    gets missed somewhere.

    Reading the flag from settings rather than a module constant is what makes
    "close it now" an environment variable instead of a deploy. It used to be a
    hardcoded `False`, which meant the one-line escape hatch the docstring
    promised was in fact a code change, a review and a release - and
    `validate_for_environment` had no way to insist on it in production. It does
    now. The real replacement remains a permission check per operation; when
    that lands, the body changes and the call sites do not move.
    """
    if get_settings().console_access_enforced:
        principal.require(Scope.ADMIN)
