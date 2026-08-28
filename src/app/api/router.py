"""Version registry and the root API router.

One place lists every version the service speaks, its lifecycle status and its
sunset date. Clients discover it at `GET /api/versions`; deprecation is
advertised on every response of a deprecated version via standard headers
(RFC 8594 `Sunset`, RFC 9745 `Deprecation`).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.api import v1
from app.core.security.dependencies import DECLARED_SECURITY_SCHEMES


class VersionStatus(StrEnum):
    #: Safe to build against.
    STABLE = "stable"
    #: Shipping, shape may still change. Not for external consumers.
    PREVIEW = "preview"
    #: Still served, but scheduled for removal. Advertised on every response.
    DEPRECATED = "deprecated"


@dataclass(frozen=True, slots=True)
class ApiVersion:
    name: str
    prefix: str
    status: VersionStatus
    router: APIRouter
    tags_metadata: list[dict[str, Any]]
    #: ISO-8601 date after which this version stops being served.
    sunset: str | None = None


#: The single source of truth for what this service speaks.
API_VERSIONS: tuple[ApiVersion, ...] = (
    ApiVersion(
        name=v1.API_VERSION,
        prefix=v1.VERSION_PREFIX,
        status=VersionStatus.STABLE,
        router=v1.router,
        tags_metadata=v1.TAGS_METADATA,
    ),
)


class ApiVersionInfo(BaseModel):
    name: str
    prefix: str = Field(description="Path prefix, relative to the API root.")
    status: VersionStatus
    sunset: str | None = Field(default=None, description="ISO date this version stops working.")


class ApiVersionsResponse(BaseModel):
    versions: list[ApiVersionInfo]
    default: str = Field(description="The version new clients should target.")


def _version_headers(version: ApiVersion) -> Callable[..., Coroutine[Any, Any, None]]:
    """Stamp every response of a version with its identity and lifecycle."""

    async def dependency(response: Response) -> None:
        response.headers["X-API-Version"] = version.name
        if version.status is VersionStatus.DEPRECATED:
            response.headers["Deprecation"] = "true"
            if version.sunset:
                response.headers["Sunset"] = version.sunset

    return dependency


def build_api_router() -> APIRouter:
    """Mount every registered version beneath the API root."""
    from fastapi import Depends

    root = APIRouter()

    @root.get(
        "/versions",
        response_model=ApiVersionsResponse,
        tags=["system"],
        summary="List the API versions this service speaks",
    )
    async def list_api_versions() -> ApiVersionsResponse:
        return ApiVersionsResponse(
            versions=[
                ApiVersionInfo(
                    name=version.name,
                    prefix=version.prefix,
                    status=version.status,
                    sunset=version.sunset,
                )
                for version in API_VERSIONS
            ],
            default=default_version().name,
        )

    for version in API_VERSIONS:
        root.include_router(
            version.router,
            prefix=version.prefix,
            dependencies=[Depends(_version_headers(version)), *DECLARED_SECURITY_SCHEMES],
        )
    return root


def default_version() -> ApiVersion:
    """The newest stable version - what new clients should generate against."""
    stable = [v for v in API_VERSIONS if v.status is VersionStatus.STABLE]
    return (stable or list(API_VERSIONS))[-1]


def all_tags_metadata() -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {
        "system": {"name": "system", "description": "Probes and service metadata."}
    }
    for version in API_VERSIONS:
        for tag in version.tags_metadata:
            seen.setdefault(tag["name"], tag)
    return list(seen.values())
