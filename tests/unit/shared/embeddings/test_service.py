"""The executor offload contract.

The model itself is never loaded here - `worker.encode_texts` is replaced. What
is under test is the thing that actually breaks: that embedding work leaves the
event loop, that ordering is preserved, and that a dimension mismatch is caught
before it reaches a `vector(384)` column.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.core.config import Settings
from app.shared.embeddings import worker
from app.shared.embeddings.service import EmbeddingService


@pytest.fixture
def patched_worker(monkeypatch):  # type: ignore[no-untyped-def]
    """Replace the torch call with a deterministic stand-in."""
    seen: dict[str, object] = {"threads": set()}

    def fake_encode(texts, model_name, torch_threads, batch_size):  # type: ignore[no-untyped-def]
        seen["threads"].add(threading.current_thread().name)  # type: ignore[union-attr]
        return [[float(len(text))] * 384 for text in texts]

    monkeypatch.setattr(worker, "encode_texts", fake_encode)
    monkeypatch.setattr(worker, "init_worker", lambda *args: None)
    return seen


def build(**overrides) -> EmbeddingService:  # type: ignore[no-untyped-def]
    settings = Settings(app_env="test", embedding_workers=2, **overrides)
    return EmbeddingService(settings)


async def test_embedding_runs_off_the_event_loop(patched_worker) -> None:
    service = build()
    service.start()
    try:
        await service.embed(["one", "two"])
    finally:
        service.shutdown()

    loop_thread = threading.current_thread().name
    assert patched_worker["threads"]
    assert loop_thread not in patched_worker["threads"], (
        "encode ran on the event-loop thread; the executor offload is broken"
    )


async def test_order_is_preserved(patched_worker) -> None:
    service = build()
    service.start()
    try:
        vectors = await service.embed(["a", "bb", "ccc"])
    finally:
        service.shutdown()

    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0]


async def test_empty_input_short_circuits(patched_worker) -> None:
    service = build()
    assert await service.embed([]) == []  # no executor needed


async def test_embedding_before_start_is_a_programming_error(patched_worker) -> None:
    with pytest.raises(RuntimeError, match="start"):
        await build().embed(["x"])


async def test_dimension_mismatch_fails_loudly(monkeypatch) -> None:
    """A model swap must not silently write vectors the column cannot hold."""
    monkeypatch.setattr(worker, "init_worker", lambda *args: None)
    monkeypatch.setattr(worker, "encode_texts", lambda texts, *a: [[0.0] * 16 for _ in texts])
    service = build()
    service.start()
    try:
        with pytest.raises(RuntimeError, match="EMBEDDING_DIM"):
            await service.embed(["x"])
    finally:
        service.shutdown()


async def test_readiness_reflects_the_pool_state(patched_worker) -> None:
    service = build()
    assert service.is_ready is False
    service.start()
    await service.warmup()
    assert service.is_ready is True
    service.shutdown()
    assert service.is_ready is False


async def test_concurrent_calls_are_all_served(patched_worker) -> None:
    service = build()
    service.start()
    try:
        results = await asyncio.gather(*(service.embed([f"text-{i}"]) for i in range(8)))
    finally:
        service.shutdown()
    assert len(results) == 8
