"""Application configuration.

Everything that differs between environments is read from the process
environment exactly once, into a cached `Settings` singleton.
`docker-compose.yaml` is the only place the API learns the database
credentials, so no secret is ever baked into an image or committed.

`validate_for_environment()` runs at startup and refuses to boot a deployment
that is dangerous rather than merely wrong - see `PRODUCTION GUARDS` below.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError
from app.core.security.principal import Scope

#: Shipped in the repository, therefore not a secret. The production guard
#: refuses to start if it is still in use.
DEFAULT_CRON_TOKEN = "local-dev-cron-token"

DEFAULT_FILTER_PROMPT = "Filter out personal messages and retain only business-related messages."

DEFAULT_SUMMARY_PROMPT = (
    "You summarize a person's communication history. Produce a concise, factual, "
    "third-person summary of at most three sentences covering recurring topics, "
    "responsibilities and open threads. Do not invent details."
)


class ApiKeyConfig(BaseModel):
    """One issued machine credential, as supplied by the environment.

    Set `API_KEYS` to a JSON array:

        [{"key": "...", "subject": "cron-scheduler", "scopes": ["ingest:run"]}]
    """

    key: SecretStr
    subject: str = Field(min_length=1, description="Stable identity for audit logs.")
    scopes: list[Scope] = Field(default_factory=list)
    tenant_id: str | None = None


class Settings(BaseSettings):
    """Typed view over the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- application -------------------------------------------------------
    app_name: str = "threadline"
    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = True
    #: Versions mount beneath this; the concrete path of v1 is /api/v1.
    api_root_prefix: str = "/api"
    #: Interactive docs. Forced off in production by the startup guard.
    docs_enabled: bool = True

    cors_origins: list[str] = ["http://localhost:3000"]
    cors_allow_credentials: bool = True

    # -- database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://threadline:change-me-in-production@localhost:5432/threadline",
        description="Async SQLAlchemy DSN. Injected by docker-compose in containers.",
    )
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800

    # -- authentication ----------------------------------------------------
    #: Legacy single-secret scheduler credential. Used only when API_KEYS is empty.
    cron_token: SecretStr = SecretStr(DEFAULT_CRON_TOKEN)
    api_keys: list[ApiKeyConfig] = Field(default_factory=list)
    #: Header-based user impersonation for local development and API tests.
    dev_auth_enabled: bool = True
    dev_auth_scopes: list[Scope] = Field(
        default_factory=lambda: [Scope.INSIGHTS_READ, Scope.MESSAGES_READ]
    )

    # -- embeddings --------------------------------------------------------
    #: Which adapter generates vectors. `ollama` is the default because it is
    #: the shape production ships in - an inference service reached over the
    #: network, with the timeouts and outages that implies. `local` keeps the
    #: in-process sentence-transformers path, for offline work and CI.
    embedding_provider: Literal["local", "ollama"] = "ollama"
    #: The contract both adapters must satisfy, and the width of the
    #: `messages.embedding` column. Changing it needs a migration: pgvector
    #: fixes the dimension on the column so the HNSW index can be built.
    #: 768 = nomic-embed-text; 384 = all-MiniLM-L6-v2.
    embedding_dim: int = 768
    embedding_warmup_on_startup: bool = True

    # Local adapter only. Ignored when EMBEDDING_PROVIDER=ollama.
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_batch_size: int = 32
    embedding_executor: Literal["thread", "process"] = "thread"
    embedding_workers: int = 1
    embedding_torch_threads: int = 2

    # -- llm ---------------------------------------------------------------
    llm_provider: Literal["stub", "anthropic", "ollama"] = "stub"
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-4-5"
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 30.0
    llm_max_concurrency: int = 4

    # -- ollama ------------------------------------------------------------
    #: Serves both the chat model (filtering, summarizing) and the embedding
    #: model. One service, two models, so there is a single place where
    #: inference happens locally - the same as pointing at one hosted provider.
    ollama_base_url: str = "http://ollama:11434"
    ollama_chat_model: str = "llama3.2:3b"
    ollama_embed_model: str = "nomic-embed-text"
    #: Generous by design. A 3B model on CPU answering a batch of filter
    #: decisions is slow, and a timeout here means the batch fails closed -
    #: messages get dropped - so it must not fire during normal operation.
    ollama_timeout_seconds: float = 300.0

    # -- fixtures ------------------------------------------------------------
    #: Stands in for Slack/Teams/GitHub during development - see
    #: `bootstrap/fixtures-service/`. Every connector in
    #: `domains/ingestion/sources.py` talks to it and maps its platform-shaped
    #: responses into `RawMessage`, the same translation a real connector
    #: would do against the real API.
    fixtures_service_url: str = "http://fixtures:8095"
    fixtures_timeout_seconds: float = 10.0

    # -- temporal ----------------------------------------------------------
    #: Durable orchestration for ingestion. The API starts a workflow and
    #: returns; the worker runs it. `enabled=false` keeps the synchronous
    #: in-request pipeline, which is what the test suite and a bare
    #: `uvicorn` run use - neither has a Temporal to talk to.
    temporal_enabled: bool = True
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    #: One inference at a time by default, matching Ollama's own
    #: `OLLAMA_NUM_PARALLEL=1`. Raising this past what the inference service
    #: can actually run in parallel just moves the queue somewhere it cannot
    #: be observed or timed out.
    temporal_max_concurrent_activities: int = 1

    # -- agent prompts -----------------------------------------------------
    ingestion_filter_system_prompt: str = DEFAULT_FILTER_PROMPT
    summary_system_prompt: str = DEFAULT_SUMMARY_PROMPT
    #: Bumped by hand whenever a prompt changes, and stored on every message the
    #: filter decides on, so a verdict can be explained after the fact.
    prompt_version: str = "v1"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the async driver, e.g. "
                "postgresql+asyncpg://user:pass@host:5432/db"
            )
        return value

    # -- derived -----------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def sync_database_url(self) -> str:
        """Blocking DSN - only for tooling that cannot speak asyncio."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    def resolved_api_keys(self) -> list[ApiKeyConfig]:
        """The credentials the API-key provider should accept.

        When `API_KEYS` is unset we synthesise one entry from `CRON_TOKEN`, so an
        existing scheduler keeps working while the scoped model becomes the
        documented path.
        """
        if self.api_keys:
            return self.api_keys
        return [
            ApiKeyConfig(
                key=self.cron_token,
                subject="cron-scheduler",
                scopes=[Scope.INGEST_RUN, Scope.INGEST_READ],
            )
        ]

    # -- PRODUCTION GUARDS -------------------------------------------------

    def validate_for_environment(self) -> None:
        """Fail fast on configurations that are unsafe rather than merely odd.

        Called from the app factory. Every check here exists because the failure
        it prevents is silent: a service that boots happily and is wide open.
        """
        problems: list[str] = []

        if self.cors_allow_credentials and "*" in self.cors_origins:
            problems.append(
                "CORS_ORIGINS contains '*' while credentials are allowed; this is "
                "invalid per the CORS spec and unsafe. List explicit origins."
            )

        for entry in self.resolved_api_keys():
            if not entry.scopes:
                problems.append(f"API key {entry.subject!r} grants no scopes and can do nothing.")

        if self.is_production:
            if self.cron_token.get_secret_value() == DEFAULT_CRON_TOKEN and not self.api_keys:
                problems.append(
                    "CRON_TOKEN is still the repository default. Set API_KEYS (preferred) "
                    "or CRON_TOKEN to a generated secret."
                )
            if self.dev_auth_enabled:
                problems.append(
                    "DEV_AUTH_ENABLED is true in production; header-based impersonation "
                    "would let anyone become any user."
                )
            if self.docs_enabled:
                problems.append("DOCS_ENABLED is true in production; disable the interactive docs.")
            if self.llm_provider == "anthropic" and not self.anthropic_api_key:
                problems.append("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY in production.")

        if problems:
            raise ConfigurationError(
                "Refusing to start: unsafe configuration.",
                details={"problems": problems},
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (the environment is read exactly once)."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings so a test can change the environment."""
    get_settings.cache_clear()
