"""Ollama embedding adapter.

Talks to Ollama's `/api/embed` over HTTP. This is the default embedder because
it has the same shape as the hosted provider this will eventually point at: a
network call to an inference service, with real timeouts and real outages.
Swapping it for OpenAI or Bedrock later is another adapter in this package and
a settings change, not a change to `domains/`.

Ollama is deliberately configured single-threaded (`OLLAMA_NUM_PARALLEL=1`), so
requests queue behind one another. That is the point: it makes the AI stage the
slow stage, which is what the durable-workflow layer exists to survive.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.shared.embeddings.base import EmbeddingError

logger = logging.getLogger(__name__)


class OllamaEmbeddingClient:
    """Adapter over Ollama's embedding endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimension: int,
        timeout_seconds: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._dim = dimension
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        #: Injected only by tests, which supply a `MockTransport` so the
        #: request/response contract can be asserted without a live Ollama.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._warm = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def is_ready(self) -> bool:
        """Transport open and at least one successful call made.

        Deliberately does not probe Ollama: readiness is checked far more often
        than the model changes, and a probe that runs inference would make the
        health endpoint as slow as the pipeline.
        """
        return self._client is not None and self._warm

    def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            )
            logger.info(
                "Ollama embedding client ready (model=%s url=%s)", self._model, self._base_url
            )

    def shutdown(self) -> None:
        # Synchronous by contract, but the transport close is a coroutine.
        # Dropping the reference is enough: httpx releases the pool on GC, and
        # the process is terminating anyway. `aclose` is offered separately for
        # callers that have a loop available.
        self._client = None
        self._warm = False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._warm = False

    async def warmup(self) -> None:
        """First call also pulls the model into memory inside Ollama."""
        await self.embed(["warmup"])
        logger.info("Ollama embedding model warm (%s).", self._model)

    # -- inference ---------------------------------------------------------

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._client is None:
            raise EmbeddingError("OllamaEmbeddingClient.start() was never called.")

        try:
            response = await self._client.post(
                "/api/embed", json={"model": self._model, "input": list(texts)}
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Ollama embedding request failed: {exc}") from exc

        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError(
                f"Ollama returned {len(vectors) if isinstance(vectors, list) else 'no'} "
                f"embeddings for {len(texts)} inputs."
            )

        if vectors and len(vectors[0]) != self._dim:
            raise EmbeddingError(
                f"Model {self._model} produced {len(vectors[0])}-d vectors but the "
                f"messages.embedding column is {self._dim}-d. Update EMBEDDING_DIM and "
                "add a migration before changing models."
            )

        self._warm = True
        return [[float(value) for value in vector] for vector in vectors]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


def build_ollama_embedding_client(settings: Settings) -> OllamaEmbeddingClient:
    return OllamaEmbeddingClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embed_model,
        dimension=settings.embedding_dim,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
