"""Embedder selection and the app-wide client singleton.

Mirrors `app.shared.llm.factory`: one place that turns configuration into an
adapter, so every caller depends on `EmbeddingClient` and nothing else has to
know which one is in play.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.shared.embeddings.base import EmbeddingClient

logger = logging.getLogger(__name__)


def build_embedding_client(settings: Settings) -> EmbeddingClient:
    if settings.embedding_provider == "local":
        from app.shared.embeddings.local import LocalEmbeddingClient

        logger.info(
            "Using the in-process embedding model (%s). No inference network calls "
            "will be made - and none of the network failure modes will be exercised.",
            settings.embedding_model_name,
        )
        return LocalEmbeddingClient(settings)

    from app.shared.embeddings.ollama import build_ollama_embedding_client

    return build_ollama_embedding_client(settings)


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    """FastAPI dependency: one client (and one executor or pool) per process."""
    return build_embedding_client(get_settings())
