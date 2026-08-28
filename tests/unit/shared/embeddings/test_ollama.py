"""The Ollama embedding adapter's contract with the wire.

No Ollama runs here. `httpx.MockTransport` answers the requests, so what is
under test is the thing that actually breaks: the request shape, the dimension
guard, and that every failure mode surfaces as `EmbeddingError` rather than a
raw `httpx` exception escaping into the pipeline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.shared.embeddings.base import EmbeddingClient, EmbeddingError
from app.shared.embeddings.ollama import OllamaEmbeddingClient

DIM = 768


def build(handler, *, dimension: int = DIM) -> OllamaEmbeddingClient:  # type: ignore[no-untyped-def]
    """A client whose transport is `handler` rather than a real socket."""
    client = OllamaEmbeddingClient(
        base_url="http://ollama:11434",
        model="nomic-embed-text",
        dimension=dimension,
        transport=httpx.MockTransport(handler),
    )
    client.start()
    return client


def vectors(count: int, dim: int = DIM):  # type: ignore[no-untyped-def]
    return httpx.Response(200, json={"embeddings": [[0.1] * dim for _ in range(count)]})


def test_it_satisfies_the_port() -> None:
    assert isinstance(build(lambda request: vectors(1)), EmbeddingClient)


async def test_it_sends_every_text_in_one_request() -> None:
    """Batching is the adapter's job; the pipeline hands it a whole list."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return vectors(3)

    result = await build(handler).embed(["a", "b", "c"])

    assert seen["url"] == "http://ollama:11434/api/embed"
    assert seen["body"] == {"model": "nomic-embed-text", "input": ["a", "b", "c"]}
    assert len(result) == 3


async def test_empty_input_makes_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    assert await build(handler).embed([]) == []


async def test_a_wrong_width_vector_is_refused() -> None:
    """The guard that stops a model swap silently corrupting the column."""
    client = build(lambda request: vectors(1, dim=384))
    with pytest.raises(EmbeddingError, match="EMBEDDING_DIM"):
        await client.embed(["x"])


async def test_a_short_reply_is_refused() -> None:
    """One vector per input, or the pipeline would mis-pair them with messages."""
    client = build(lambda request: vectors(2))
    with pytest.raises(EmbeddingError, match="embeddings for 3 inputs"):
        await client.embed(["a", "b", "c"])


async def test_a_transport_failure_becomes_an_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(EmbeddingError, match="Ollama embedding request failed"):
        await build(handler).embed(["x"])


async def test_an_http_error_becomes_an_embedding_error() -> None:
    client = build(lambda request: httpx.Response(503, text="model loading"))
    with pytest.raises(EmbeddingError, match="Ollama embedding request failed"):
        await client.embed(["x"])


async def test_embedding_before_start_is_a_programming_error() -> None:
    client = OllamaEmbeddingClient(base_url="http://ollama:11434", model="m", dimension=DIM)
    with pytest.raises(EmbeddingError, match="start"):
        await client.embed(["x"])


async def test_readiness_needs_a_successful_call_not_just_a_transport() -> None:
    """Otherwise the probe would report ready while Ollama is still pulling."""
    client = build(lambda request: vectors(1))
    assert client.is_ready is False
    await client.embed(["x"])
    assert client.is_ready is True


async def test_shutdown_makes_it_unready() -> None:
    client = build(lambda request: vectors(1))
    await client.embed(["x"])
    client.shutdown()
    assert client.is_ready is False
