"""Connection to the Temporal frontend.

One client per process, cached. The pydantic data converter is set here rather
than per call: every payload in `workflows/dto.py` is a pydantic model, and
Temporal's default JSON converter cannot round-trip them (it would hand a
workflow plain dicts on replay, which fails in a way that only shows up after
a worker restart).
"""

from __future__ import annotations

import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_client: Client | None = None


async def connect(settings: Settings | None = None) -> Client:
    """Open the shared client, or return the one already open."""
    global _client
    if _client is not None:
        return _client

    resolved = settings or get_settings()
    _client = await Client.connect(
        resolved.temporal_address,
        namespace=resolved.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    logger.info(
        "connected to Temporal at %s (namespace=%s)",
        resolved.temporal_address,
        resolved.temporal_namespace,
    )
    return _client


async def close() -> None:
    global _client
    _client = None


def reset_client_cache() -> None:
    """Drop the cached client. Test-support only."""
    global _client
    _client = None
