"""FastAPI wiring for authentication and scope checks."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Security
from starlette.requests import Request

from app.core.config import get_settings
from app.core.security.principal import Principal, Scope
from app.core.security.providers import (
    AuthenticationChain,
    api_key_scheme,
    build_auth_chain,
    dev_user_scheme,
)

#: Attach to a router (or the app) so Swagger UI shows an "Authorize" dialog
#: for both header schemes. Cosmetic only - see `providers.py`.
DECLARED_SECURITY_SCHEMES = (Security(api_key_scheme), Security(dev_user_scheme))


@lru_cache(maxsize=1)
def get_auth_chain() -> AuthenticationChain:
    return build_auth_chain(get_settings())


async def get_principal(request: Request) -> Principal:
    """Resolve the caller. Always returns a Principal, never None.

    An unauthenticated request gets `Principal.anonymous()`, whose `require()`
    raises 401 - so a handler that forgets to check scopes fails closed rather
    than treating `None` as "fine".
    """
    principal = await get_auth_chain().resolve(request)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_scopes(*scopes: Scope) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Dependency factory for router- or route-level scope enforcement.

    router = APIRouter(dependencies=[Depends(require_scopes(Scope.INGEST_RUN))])
    """

    async def dependency(principal: CurrentPrincipal) -> Principal:
        principal.require(*scopes)
        return principal

    return dependency


def ScopedPrincipal(*scopes: Scope) -> Any:
    """Annotated type that both enforces scopes and injects the principal.

    async def handler(principal: ScopedPrincipal(Scope.INSIGHTS_READ)) -> ...
    """
    return Annotated[Principal, Depends(require_scopes(*scopes))]


def reset_auth_cache() -> None:
    """Drop the cached chain so a test can change settings. Test-support only."""
    get_auth_chain.cache_clear()
