"""Provider selection.

The factory is one `if`, but it is the seam that decides whether the shipped
deployment talks to a network service or loads torch into the API process -
worth pinning so a default cannot drift silently.
"""

from __future__ import annotations

from app.core.config import Settings
from app.shared.embeddings.base import EmbeddingClient
from app.shared.embeddings.factory import build_embedding_client
from app.shared.embeddings.ollama import OllamaEmbeddingClient


def settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(app_env="test", **overrides)


def test_ollama_is_the_default() -> None:
    """The default must be the shape production ships in, not the convenient one."""
    client = build_embedding_client(settings())
    assert isinstance(client, OllamaEmbeddingClient)
    assert isinstance(client, EmbeddingClient)


def test_the_local_adapter_is_still_reachable() -> None:
    """Kept as the offline path; deleting it would strand anyone without Ollama."""
    from app.shared.embeddings.local import LocalEmbeddingClient

    client = build_embedding_client(settings(embedding_provider="local", embedding_dim=384))
    assert isinstance(client, LocalEmbeddingClient)
    assert isinstance(client, EmbeddingClient)


def test_the_configured_dimension_reaches_the_client() -> None:
    """EMBEDDING_DIM is the contract with the column; a factory that dropped it
    would produce vectors the database silently refuses."""
    assert build_embedding_client(settings(embedding_dim=768)).dimension == 768


def test_the_configured_model_reaches_the_client() -> None:
    client = build_embedding_client(settings(ollama_embed_model="mxbai-embed-large"))
    assert client.model_name == "mxbai-embed-large"
