"""Provider selection and the app-wide client singleton."""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.shared.llm.base import LLMClient
from app.shared.llm.stub import StubLLMClient

logger = logging.getLogger(__name__)


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "ollama":
        from app.shared.llm.ollama_client import OllamaLLMClient

        logger.info(
            "Using Ollama (%s) at %s.", settings.ollama_chat_model, settings.ollama_base_url
        )
        return OllamaLLMClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_chat_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.ollama_timeout_seconds,
        )

    if settings.llm_provider == "anthropic":
        api_key = (
            settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
        )
        if not api_key:
            if settings.is_production:
                raise RuntimeError(
                    "LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY in production."
                )
            logger.warning(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty - "
                "falling back to the offline stub provider."
            )
            return StubLLMClient()

        from app.shared.llm.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(
            api_key=api_key,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    logger.info("Using the offline stub LLM provider (no external calls will be made).")
    return StubLLMClient()


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """FastAPI dependency: one client per process, reused across requests."""
    return build_llm_client(get_settings())


async def close_llm_client() -> None:
    if get_llm_client.cache_info().currsize:
        await get_llm_client().aclose()
