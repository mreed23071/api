"""Anthropic Messages API adapter."""

from __future__ import annotations

import logging

from app.shared.llm.base import LLMError, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class AnthropicLLMClient:
    """Thin adapter over `anthropic.AsyncAnthropic`.

    Only message *metadata* and text reach this client, and only when an
    operator explicitly opts in via LLM_PROVIDER=anthropic. Embeddings never do:
    those are generated locally by design.
    """

    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = 1024,
        timeout_seconds: float = 30.0,
    ) -> None:
        from anthropic import AsyncAnthropic

        self.model = model
        self._max_tokens = max_tokens
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        from anthropic import APIError

        try:
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens or self._max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
            )
        except APIError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise LLMError("Anthropic returned an empty completion.")
        return LLMResponse(text=text, provider=self.provider, model=self.model)

    async def aclose(self) -> None:
        await self._client.close()
