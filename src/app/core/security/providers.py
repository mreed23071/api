"""Authentication adapters.

`AuthProvider` is the port. Adding OIDC later means writing one more class here
and listing it in `build_auth_chain` - no route, service or test changes.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fastapi.security.api_key import APIKeyHeader
from starlette.requests import Request

from app.core.errors import AuthenticationError
from app.core.security.principal import Principal, PrincipalKind, Scope, TenantContext

if TYPE_CHECKING:  # import for typing only - keeps config out of the import graph
    from app.core.config import Settings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
#: The prototype's original scheduler header. Accepted so existing cron entries
#: keep working; remove once callers have migrated to X-API-Key.
LEGACY_CRON_HEADER = "X-Cron-Token"
DEV_USER_HEADER = "X-Dev-User"
DEV_SCOPES_HEADER = "X-Dev-Scopes"

# Declared purely so FastAPI registers a `securitySchemes` entry and Swagger UI
# renders an "Authorize" dialog. The actual credential check still happens in
# `AuthProvider.authenticate()` against the raw header - these schemes never
# gate a request themselves (`auto_error=False`), they just document one.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    scheme_name="ApiKey",
    auto_error=False,
    description=(
        "Service credential for machine callers (scheduler, backend jobs). "
        "The legacy `X-Cron-Token` header is also accepted. Grants whatever "
        "scopes were issued with the key, e.g. `ingest:run`."
    ),
)
dev_user_scheme = APIKeyHeader(
    name=DEV_USER_HEADER,
    scheme_name="DevUser",
    auto_error=False,
    description=(
        "Local development only: impersonate a user by name. Defaults to "
        "`insights:read, messages:read`; add the `X-Dev-Scopes` header "
        "(comma-separated) to override. Disabled whenever DEV_AUTH_ENABLED=false."
    ),
)


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """One issued machine credential."""

    secret: str
    subject: str
    scopes: frozenset[Scope]
    tenant_id: str | None = None


@runtime_checkable
class AuthProvider(Protocol):
    """Resolve a request into a principal, or decline to handle it."""

    scheme: str

    async def authenticate(self, request: Request) -> Principal | None:
        """Return a principal, or None if this scheme was not presented.

        Raise `AuthenticationError` when the scheme *was* presented but the
        credential is bad - falling through to the next provider would let a
        caller probe every scheme with one request.
        """
        ...


class ApiKeyAuthProvider:
    """Static shared secrets for machine callers.

    A stopgap with known limits - no expiry, no rotation, no revocation list -
    but it produces a real `Principal` with real scopes, so everything
    downstream is already written against the interface a proper IdP will
    satisfy. See `docs/ARCHITECTURE.md` for the migration path.
    """

    scheme = "ApiKey"

    def __init__(self, records: Sequence[ApiKeyRecord]) -> None:
        self._records = tuple(records)

    async def authenticate(self, request: Request) -> Principal | None:
        presented = request.headers.get(API_KEY_HEADER) or request.headers.get(
            LEGACY_CRON_HEADER
        )
        if not presented:
            return None

        # Compare against every record rather than breaking on the first match,
        # so the time taken does not reveal which key was hit.
        matched: ApiKeyRecord | None = None
        for record in self._records:
            if hmac.compare_digest(record.secret, presented):
                matched = record

        if matched is None:
            raise AuthenticationError("Invalid API key.")

        return Principal(
            subject=matched.subject,
            kind=PrincipalKind.SERVICE,
            scopes=matched.scopes,
            tenant=TenantContext(tenant_id=matched.tenant_id),
            display_name=matched.subject,
            auth_scheme=self.scheme,
        )


class DevUserAuthProvider:
    """Impersonate a user from a header. Never enabled in production.

    Exists so the frontend and the API tests can exercise user-scoped routes
    before an identity provider is chosen. `Settings` refuses to start with this
    enabled when APP_ENV=production.
    """

    scheme = "DevUser"

    def __init__(self, default_scopes: frozenset[Scope]) -> None:
        self._default_scopes = default_scopes

    async def authenticate(self, request: Request) -> Principal | None:
        subject = request.headers.get(DEV_USER_HEADER)
        if not subject:
            return None

        raw_scopes = request.headers.get(DEV_SCOPES_HEADER)
        if raw_scopes:
            try:
                scopes = frozenset(
                    Scope.parse(part) for part in raw_scopes.split(",") if part.strip()
                )
            except ValueError as exc:
                raise AuthenticationError(f"Invalid {DEV_SCOPES_HEADER}: {exc}") from exc
        else:
            scopes = self._default_scopes

        return Principal(
            subject=subject,
            kind=PrincipalKind.USER,
            scopes=scopes,
            tenant=TenantContext.global_scope(),
            display_name=subject,
            auth_scheme=self.scheme,
        )


class AuthenticationChain:
    """Try each provider in order; fall back to an anonymous principal."""

    def __init__(self, providers: Sequence[AuthProvider]) -> None:
        self._providers = tuple(providers)

    @property
    def schemes(self) -> tuple[str, ...]:
        return tuple(provider.scheme for provider in self._providers)

    async def resolve(self, request: Request) -> Principal:
        for provider in self._providers:
            principal = await provider.authenticate(request)
            if principal is not None:
                return principal
        return Principal.anonymous()


def build_auth_chain(settings: Settings) -> AuthenticationChain:
    providers: list[AuthProvider] = [
        ApiKeyAuthProvider(
            [
                ApiKeyRecord(
                    secret=entry.key.get_secret_value(),
                    subject=entry.subject,
                    scopes=frozenset(entry.scopes),
                    tenant_id=entry.tenant_id,
                )
                for entry in settings.resolved_api_keys()
            ]
        )
    ]
    if settings.dev_auth_enabled:
        logger.warning(
            "Header-based dev authentication is ENABLED (%s). Never use this in production.",
            DEV_USER_HEADER,
        )
        providers.append(DevUserAuthProvider(frozenset(settings.dev_auth_scopes)))
    return AuthenticationChain(providers)
