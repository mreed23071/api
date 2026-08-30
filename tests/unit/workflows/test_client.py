"""The shared Temporal client is constructed exactly once per process.

`connect()` awaits between checking the global and assigning it, and an `await`
is a yield point. N callers arriving before the first one finished each saw
`None`, each built a client, and N-1 of those were overwritten - each holding a
gRPC connection with no remaining reference to close it. On a cold API start
that N is "however many requests arrived in the first second".
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.workflows import client as client_module

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_client_cache():  # type: ignore[no-untyped-def]
    client_module.reset_client_cache()
    yield
    client_module.reset_client_cache()


class FakeClient:
    """Stands in for `temporalio.client.Client`, counting constructions."""

    constructed = 0

    def __init__(self) -> None:
        type(self).constructed += 1
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_fifty_concurrent_callers_construct_one_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Acceptance check H8."""
    FakeClient.constructed = 0

    async def fake_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        # A real connect does I/O; the sleep is what makes the race reproducible
        # rather than dependent on scheduling luck.
        await asyncio.sleep(0.01)
        return FakeClient()

    monkeypatch.setattr(client_module.Client, "connect", staticmethod(fake_connect))

    settings = Settings()
    clients = await asyncio.gather(*(client_module.connect(settings) for _ in range(50)))

    assert FakeClient.constructed == 1, (
        f"{FakeClient.constructed} clients were built; {FakeClient.constructed - 1} leaked"
    )
    # And every caller got the same one, not just "one was built".
    assert len({id(c) for c in clients}) == 1


async def test_a_failed_connect_leaves_no_client_behind(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The global is assigned only after construction fully succeeds, so a
    failure is retried cleanly rather than caching a half-built object."""

    async def failing_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("temporal is down")

    monkeypatch.setattr(client_module.Client, "connect", staticmethod(failing_connect))

    with pytest.raises(ConnectionError):
        await client_module.connect(Settings())

    assert client_module._client is None


async def test_a_failed_connect_does_not_wedge_the_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A caller after a failure must still be able to try.

    `async with` releases on the exception path, but asserting it means a future
    refactor into manual acquire/release cannot quietly deadlock every later
    caller behind a lock nobody holds a reference to.
    """
    FakeClient.constructed = 0
    attempts = {"n": 0}

    async def flaky_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("temporal is down")
        return FakeClient()

    monkeypatch.setattr(client_module.Client, "connect", staticmethod(flaky_connect))

    with pytest.raises(ConnectionError):
        await client_module.connect(Settings())

    revived = await asyncio.wait_for(client_module.connect(Settings()), timeout=2)
    assert isinstance(revived, FakeClient)


async def test_close_releases_the_client_and_clears_the_global(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    FakeClient.constructed = 0

    async def fake_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakeClient()

    monkeypatch.setattr(client_module.Client, "connect", staticmethod(fake_connect))

    opened = await client_module.connect(Settings())
    await client_module.close()

    assert opened.closed is True
    assert client_module._client is None


async def test_close_tolerates_an_sdk_with_no_teardown_method(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`Client`'s teardown surface varies by SDK version; some expose nothing.

    Dropping the reference is acceptable *because* construction is single-flight
    - at most one client exists per process, so there is no orphan to leak.
    """

    class Opaque:
        pass

    async def fake_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        return Opaque()

    monkeypatch.setattr(client_module.Client, "connect", staticmethod(fake_connect))

    await client_module.connect(Settings())
    await client_module.close()  # must not raise

    assert client_module._client is None
