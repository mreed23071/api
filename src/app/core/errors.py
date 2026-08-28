"""The application's exception hierarchy.

Every failure the domain can express is one of these. The API layer maps them
to HTTP once, in `app.api.errors`, so services never import `fastapi` and never
decide on a status code. That is what keeps the domain layer reusable from a
worker, a CLI or a test without a request in scope.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers.

    These are part of the public API contract: clients branch on them. Add new
    members freely; never rename or repurpose an existing one.
    """

    INTERNAL = "internal_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION_FAILED = "validation_failed"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_FAILED = "upstream_failed"
    UNAVAILABLE = "service_unavailable"
    CONFIGURATION = "configuration_error"


class AppError(Exception):
    """Base class for every expected failure.

    `status_code` and `code` are class attributes so a subclass is a complete
    declaration; instances may override them for one-off cases.
    """

    status_code: int = 500
    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    """A uniqueness or state precondition was violated."""

    status_code = 409
    code = ErrorCode.CONFLICT


class ValidationError(AppError):
    """Input was well-formed but semantically invalid for the domain."""

    status_code = 422
    code = ErrorCode.VALIDATION_FAILED


class AuthenticationError(AppError):
    status_code = 401
    code = ErrorCode.UNAUTHENTICATED


class AuthorizationError(AppError):
    """Authenticated, but the principal lacks the required scope."""

    status_code = 403
    code = ErrorCode.FORBIDDEN


class RateLimitedError(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED


class UpstreamError(AppError):
    """A dependency we do not control failed - LLM provider, connector, etc."""

    status_code = 502
    code = ErrorCode.UPSTREAM_FAILED


class ServiceUnavailableError(AppError):
    status_code = 503
    code = ErrorCode.UNAVAILABLE


class ConfigurationError(AppError):
    """Raised at startup when the deployment is misconfigured. Never returned."""

    status_code = 500
    code = ErrorCode.CONFIGURATION
