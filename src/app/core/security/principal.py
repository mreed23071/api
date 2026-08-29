"""Who is calling, and what they are allowed to do.

The `Principal` is the object every other design decision hangs off. It is
resolved once at the edge and passed *into* the service layer, so authorization
is a domain concern with domain vocabulary rather than a boolean check at the
door that nothing downstream can see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.errors import AuthenticationError, AuthorizationError


class Scope(StrEnum):
    """Capabilities a principal can hold.

    Scopes name *actions*, not roles. Roles are a mapping from a name to a set
    of scopes and belong in whichever identity provider issues credentials -
    keeping them out of here is what lets the IdP change without a code change.
    """

    INGEST_RUN = "ingest:run"
    INGEST_READ = "ingest:read"
    INSIGHTS_READ = "insights:read"
    MESSAGES_READ = "messages:read"
    ADMIN = "admin"

    @classmethod
    def parse(cls, raw: str) -> Scope:
        try:
            return cls(raw.strip())
        except ValueError as exc:
            raise ValueError(f"unknown scope {raw!r}; valid: {[s.value for s in cls]}") from exc


class PrincipalKind(StrEnum):
    #: A machine: the scheduler, a backend service, another system.
    SERVICE = "service"
    #: A human, authenticated through an identity provider.
    USER = "user"
    #: Nobody. Carried by unauthenticated requests so that route handlers always
    #: receive a Principal and never a None they might forget to check.
    ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The isolation boundary a principal is acting within.

    mabinsoft is single-tenant today, so every principal gets `global_scope()`.
    The type exists now because retrofitting a tenant argument through every
    repository later is the expensive version of this change; retrofitting the
    *body* of `Repository.scoped()` is the cheap one.
    """

    tenant_id: str | None = None

    @classmethod
    def global_scope(cls) -> TenantContext:
        """No tenant restriction - the only mode implemented today."""
        return cls(tenant_id=None)

    @property
    def is_global(self) -> bool:
        return self.tenant_id is None


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated (or explicitly anonymous) caller."""

    subject: str
    kind: PrincipalKind
    scopes: frozenset[Scope] = field(default_factory=frozenset)
    tenant: TenantContext = field(default_factory=TenantContext.global_scope)
    display_name: str | None = None
    #: Which auth scheme produced this principal - useful in audit logs.
    auth_scheme: str = "none"

    @classmethod
    def anonymous(cls) -> Principal:
        return cls(subject="anonymous", kind=PrincipalKind.ANONYMOUS, auth_scheme="none")

    @property
    def is_authenticated(self) -> bool:
        return self.kind is not PrincipalKind.ANONYMOUS

    def has(self, scope: Scope) -> bool:
        return Scope.ADMIN in self.scopes or scope in self.scopes

    def require(self, *scopes: Scope) -> None:
        """Assert every listed scope, raising the right error for the caller.

        Anonymous callers get 401 (you have not identified yourself);
        authenticated ones get 403 (you have, and it is not enough). Conflating
        the two is how APIs end up leaking whether a resource exists.
        """
        if not self.is_authenticated:
            raise AuthenticationError(
                "Authentication is required for this operation.",
                details={"required_scopes": [scope.value for scope in scopes]},
            )
        missing = [scope.value for scope in scopes if not self.has(scope)]
        if missing:
            raise AuthorizationError(
                "The authenticated principal lacks the required scope.",
                details={"missing_scopes": missing, "subject": self.subject},
            )

    def audit_fields(self) -> dict[str, str]:
        """Structured-log fields identifying this caller."""
        return {
            "principal_subject": self.subject,
            "principal_kind": self.kind.value,
            "auth_scheme": self.auth_scheme,
        }
