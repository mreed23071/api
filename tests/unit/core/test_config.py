"""The production guards. Each of these prevents a silently wide-open service."""

from __future__ import annotations

import pytest

from app.core.config import DEFAULT_CRON_TOKEN, ApiKeyConfig, Settings
from app.core.errors import ConfigurationError
from app.core.security.principal import Scope


def build(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    defaults = {
        "app_env": "production",
        "database_url": "postgresql+asyncpg://u:p@db:5432/threadline",
        "dev_auth_enabled": False,
        "docs_enabled": False,
        "api_keys": [
            ApiKeyConfig(key="generated-secret", subject="cron", scopes=[Scope.INGEST_RUN])
        ],
    }
    return Settings(**{**defaults, **overrides})


def test_a_correctly_configured_production_deployment_starts() -> None:
    build().validate_for_environment()


def test_rejects_a_sync_database_url() -> None:
    with pytest.raises(ValueError, match="async driver"):
        build(database_url="postgresql://u:p@db:5432/threadline")


def test_rejects_the_default_cron_token_in_production() -> None:
    settings = build(api_keys=[], cron_token=DEFAULT_CRON_TOKEN)
    with pytest.raises(ConfigurationError) as excinfo:
        settings.validate_for_environment()
    assert any("CRON_TOKEN" in problem for problem in excinfo.value.details["problems"])


def test_rejects_dev_auth_in_production() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build(dev_auth_enabled=True).validate_for_environment()
    assert any("DEV_AUTH_ENABLED" in p for p in excinfo.value.details["problems"])


def test_rejects_public_docs_in_production() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build(docs_enabled=True).validate_for_environment()
    assert any("DOCS_ENABLED" in p for p in excinfo.value.details["problems"])


def test_rejects_anthropic_without_a_key_in_production() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build(llm_provider="anthropic").validate_for_environment()
    assert any("ANTHROPIC_API_KEY" in p for p in excinfo.value.details["problems"])


def test_rejects_wildcard_cors_with_credentials_in_every_environment() -> None:
    settings = Settings(app_env="local", cors_origins=["*"], cors_allow_credentials=True)
    with pytest.raises(ConfigurationError) as excinfo:
        settings.validate_for_environment()
    assert any("CORS_ORIGINS" in p for p in excinfo.value.details["problems"])


def test_rejects_a_key_that_grants_nothing() -> None:
    settings = build(api_keys=[ApiKeyConfig(key="k", subject="useless", scopes=[])])
    with pytest.raises(ConfigurationError):
        settings.validate_for_environment()


def test_cron_token_synthesises_a_scoped_key_when_api_keys_is_empty() -> None:
    settings = Settings(app_env="local", api_keys=[], cron_token="abc")
    keys = settings.resolved_api_keys()
    assert len(keys) == 1
    assert keys[0].subject == "cron-scheduler"
    assert set(keys[0].scopes) == {Scope.INGEST_RUN, Scope.INGEST_READ}


def test_explicit_api_keys_replace_the_legacy_token() -> None:
    settings = build()
    assert [k.subject for k in settings.resolved_api_keys()] == ["cron"]
