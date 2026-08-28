"""The embedding port.

Ingestion depends on this protocol, never on a concrete embedder. That is what
makes the embedder a deployment decision rather than a code change - the same
treatment `LLMClient` already gets in `app.shared.llm.base`.

The port exists because the in-process model this service started with is not
what ships. Production reaches an inference service over the network, which
fails in ways a thread pool never does: timeouts, refused connections, 503s.
Keeping the boundary here means those failure modes are exercised locally
(against Ollama) and handled once, instead of being discovered at go-to-market.

`start`/`shutdown`/`warmup` are lifecycle hooks the API's lifespan drives. A
network-backed adapter has little to do in them; the local one loads a torch
model, which is exactly why they are on the port rather than assumed away.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(RuntimeError):
    """Raised when the embedder fails or returns something unusable."""


@runtime_checkable
class EmbeddingClient(Protocol):
    """Port implemented by every embedding adapter."""

    @property
    def dimension(self) -> int:
        """Vector width. Must match `messages.embedding`, or writes fail."""
        ...

    @property
    def model_name(self) -> str:
        """Recorded on every embedded row, so a vector can be explained later."""
        ...

    @property
    def is_ready(self) -> bool:
        """Whether the readiness probe should report embeddings as healthy.

        Must not itself perform inference - the probe is called often.
        """
        ...

    def start(self) -> None:
        """Acquire resources. Called once, during application startup."""
        ...

    def shutdown(self) -> None:
        """Release resources. Called once, during application shutdown."""
        ...

    async def warmup(self) -> None:
        """Pay any first-call cost up front, so no request has to."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed `texts`, preserving order. Raises `EmbeddingError` on failure."""
        ...

    async def embed_one(self, text: str) -> list[float]:
        """Embed a single string."""
        ...
