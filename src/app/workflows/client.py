"""Connection to the Temporal frontend.

One client per process, cached. The pydantic data converter is set here rather
than per call: every payload in `workflows/dto.py` is a pydantic model, and
Temporal's default JSON converter cannot round-trip them (it would hand a
workflow plain dicts on replay, which fails in a way that only shows up after
a worker restart).
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_client: Client | None = None

#: Serialises construction, not use. Constructed at import: `asyncio.Lock()` no
#: longer binds an event loop at creation time (Python 3.10+), and this project
#: requires 3.12 - the workflow module uses PEP 695 generics.
_lock = asyncio.Lock()


async def connect(settings: Settings | None = None) -> Client:
    """Open the shared client, or return the one already open.

    Single-flight, double-checked. The unguarded version raced: `connect` awaits
    inside the gap between its `is not None` test and its assignment, so N
    concurrent first callers - the API serving N requests after a cold start,
    say - each saw `None`, each built a client, and N-1 of them were overwritten
    and leaked, holding gRPC connections nobody could ever close.
    """
    global _client
    if _client is not None:  # fast path, no lock
        return _client

    async with _lock:
        if _client is not None:  # second check, under the lock
            return _client

        resolved = settings or get_settings()
        client = await Client.connect(
            resolved.temporal_address,
            namespace=resolved.temporal_namespace,
            data_converter=pydantic_data_converter,
        )
        logger.info(
            "connected to Temporal at %s (namespace=%s)",
            resolved.temporal_address,
            resolved.temporal_namespace,
        )
        # Assign only after construction fully succeeds, so a failed connect
        # leaves the global untouched and the next caller retries cleanly.
        _client = client
        return _client


async def close() -> None:
    """Drop the shared client, releasing its transport if the SDK exposes a way.

    `temporalio.client.Client`'s teardown surface varies by SDK version and some
    versions expose nothing public, hence the probe. Dropping the reference is
    an acceptable fallback here specifically because `connect` is single-flight:
    at most one client is ever constructed per process, so there is no orphan to
    leak - which was not true before.
    """
    global _client
    async with _lock:
        client, _client = _client, None

    if client is None:
        return

    for name in ("close", "disconnect", "aclose"):
        teardown = getattr(client, name, None)
        if teardown is None:
            continue
        result = teardown()
        if asyncio.iscoroutine(result):
            await result
        return


def reset_client_cache() -> None:
    """Drop the cached client. Test-support only."""
    global _client
    _client = None
