"""ASGI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import system
from app.api.errors import register_exception_handlers
from app.api.router import all_tags_metadata, build_api_router
from app.core.config import Settings, get_settings
from app.core.db.engine import dispose_engine
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.openapi import assert_unique_operation_ids, custom_generate_unique_id
from app.core.security.dependencies import get_auth_chain
from app.shared.embeddings.factory import get_embedding_client
from app.shared.llm.factory import close_llm_client, get_llm_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the expensive, process-wide resources.

    The embedding executor and its model start here - once per process, before
    the first request - rather than lazily on the hot path.
    """
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    embeddings = get_embedding_client()
    embeddings.start()
    if settings.embedding_warmup_on_startup:
        await embeddings.warmup()

    llm = get_llm_client()
    logger.info(
        "application started",
        extra={
            "environment": settings.app_env,
            "llm_provider": llm.provider,
            "embedding_provider": settings.embedding_provider,
            "embedding_model": embeddings.model_name,
            "auth_schemes": list(get_auth_chain().schemes),
        },
    )

    try:
        yield
    finally:
        await close_llm_client()
        embeddings.shutdown()
        await dispose_engine()
        logger.info("shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Accepts settings so a test can construct an app for a specific
    configuration without mutating the process environment.
    """
    settings = settings or get_settings()
    # Refuse to boot an unsafe deployment rather than serving one.
    settings.validate_for_environment()

    docs_enabled = settings.docs_enabled and not settings.is_production

    app = FastAPI(
        title="threadline API",
        version=__version__,
        summary="Multi-platform message ingestion, private embeddings and agentic summaries.",
        description=(
            "Versioned under `/api/{version}`. Probes (`/health`, `/ready`) are "
            "unversioned infrastructure contracts. Every error shares one envelope; "
            "see the `ErrorResponse` schema."
        ),
        lifespan=lifespan,
        openapi_tags=all_tags_metadata(),
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        # Clean, ergonomic method names in the generated TypeScript SDK.
        generate_unique_id_function=custom_generate_unique_id,
    )

    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            # Custom response headers are invisible to browser JS unless
            # listed here - without this, `GET /users?limit=...`'s pagination
            # metadata would be readable in curl but silently absent in fetch.
            expose_headers=["X-Total-Count", "X-Has-More"],
        )

    register_exception_handlers(app, auth_schemes=get_auth_chain().schemes)

    app.include_router(system.router)
    app.include_router(build_api_router(), prefix=settings.api_root_prefix)

    # Fail at build time, not at SDK-generation time.
    assert_unique_operation_ids(app)
    return app


app = create_app()
