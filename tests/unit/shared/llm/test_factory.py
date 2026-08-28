"""Provider selection - including the guard that stops production silently
degrading to a keyword heuristic."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.shared.llm.factory import build_llm_client
from app.shared.llm.stub import StubLLMClient


def test_default_provider_is_the_offline_stub() -> None:
    client = build_llm_client(Settings(app_env="local"))
    assert isinstance(client, StubLLMClient)


def test_missing_key_falls_back_to_the_stub_outside_production() -> None:
    client = build_llm_client(
        Settings(app_env="local", llm_provider="anthropic", anthropic_api_key=None)
    )
    assert isinstance(client, StubLLMClient)


def test_missing_key_is_fatal_in_production() -> None:
    """Silently summarising production data with a heuristic would be worse."""
    settings = Settings(
        app_env="production",
        llm_provider="anthropic",
        anthropic_api_key=None,
        database_url="postgresql+asyncpg://u:p@db:5432/m",
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm_client(settings)


def test_every_adapter_declares_provider_and_model() -> None:
    client = build_llm_client(Settings(app_env="local"))
    assert isinstance(client.provider, str) and isinstance(client.model, str)
