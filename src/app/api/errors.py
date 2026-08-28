"""One error envelope, one place that maps failures to HTTP.

Every error response in every version has the same shape, so the generated
TypeScript client has exactly one error type to narrow against:

    { "error": { "code": "...", "message": "...", "details": {...},
                 "request_id": "..." } }
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode
from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: ErrorCode = Field(description="Stable machine-readable identifier.")
    message: str = Field(description="Human-readable explanation. Not for parsing.")
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(
        default=None, description="Correlates this response with the server logs."
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


#: Attach to routers so the schema documents the envelope, and the SDK types it.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed request."},
    401: {"model": ErrorResponse, "description": "Authentication required or invalid."},
    403: {"model": ErrorResponse, "description": "Authenticated but not permitted."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    422: {"model": ErrorResponse, "description": "Request failed validation."},
    500: {"model": ErrorResponse, "description": "Unexpected server error."},
}

AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    key: ERROR_RESPONSES[key] for key in (401, 403)
}


def _request_id(request: Request | None) -> str | None:
    """Prefer request state over the contextvar.

    The catch-all `Exception` handler runs in Starlette's ServerErrorMiddleware,
    which sits *outside* our request-context middleware - by then the contextvar
    has already been reset. `request.state` survives, so a 500 still carries an
    id the reporter can quote.
    """
    if request is not None:
        state_id = getattr(request.state, "request_id", None)
        if state_id:
            return str(state_id)
    return request_id_var.get()


def _envelope(
    request: Request | None,
    status_code: int,
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details or {},
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI, *, auth_schemes: tuple[str, ...] = ()) -> None:
    """Install the handlers. Order does not matter; FastAPI dispatches by type."""

    challenge = ", ".join(auth_schemes) or "ApiKey"

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": challenge} if exc.status_code == 401 else None
        if exc.status_code >= 500:
            logger.exception("unhandled application error", extra={"error_code": exc.code.value})
        else:
            logger.info(
                "request rejected",
                extra={"error_code": exc.code.value, "http_status": exc.status_code},
            )
        return _envelope(
            request, exc.status_code, exc.code, exc.message, exc.details, headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # A field validator that raises ValueError leaves the raw exception in
        # errors()[i]["ctx"]["error"] - not JSON-serializable, and
        # payload.model_dump(mode="json") has no fallback for it. jsonable_encoder
        # is what FastAPI's own default handler uses to sanitize the same payload.
        return _envelope(
            request,
            422,
            ErrorCode.VALIDATION_FAILED,
            "Request failed validation.",
            {"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Starlette raises these for 404s on unknown paths and 405s, so they must
        # land in the same envelope as everything else.
        code = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL)
        return _envelope(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        # Deliberately opaque: an unexpected exception's message can carry
        # connection strings, row contents or provider payloads.
        return _envelope(
            request,
            500,
            ErrorCode.INTERNAL,
            "An unexpected error occurred. Quote the request id when reporting it.",
        )
