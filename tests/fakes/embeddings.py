"""A deterministic stand-in for the local embedding model.

Imports no torch, downloads nothing, and returns stable vectors so an
assertion on an embedding is reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


class FakeEmbeddingService:
    """Duck-types `app.shared.embeddings.service.EmbeddingService`."""

    def __init__(self, *, dim: int = 384, model_name: str = "fake-embedder-v1") -> None:
        self._dim = dim
        self._model_name = model_name
        self._started = False
        self.calls: list[list[str]] = []

    # -- the EmbeddingService surface --------------------------------------

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_ready(self) -> bool:
        return self._started

    def start(self) -> None:
        self._started = True

    def shutdown(self) -> None:
        self._started = False

    async def warmup(self) -> None:
        self._started = True

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    # -- internals ---------------------------------------------------------

    def _vector(self, text: str) -> list[float]:
        """A stable pseudo-embedding: same text in, same vector out."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(self._dim)]
        norm = sum(value * value for value in raw) ** 0.5 or 1.0
        return [value / norm for value in raw]
