"""Ollama chat adapter.

Stands in for a hosted provider during development: same port, same network
shape, no tokens billed. Points at the same Ollama service that serves
embeddings.

The reason this adapter is more than a URL swap is `response_schema`. A 3B
model asked politely for JSON will sometimes answer with prose, a fenced block,
or a decision list that is missing entries - and `MessageFilterAgent` fails
closed, so a malformed answer silently *discards messages*. Ollama's `format`
parameter constrains decoding to a JSON schema, which turns "usually valid
JSON" into "valid JSON by construction". Without it a small local model is not
usable for the filter at all.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.shared.llm.base import LLMError, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class OllamaLLMClient:
    """Adapter over Ollama's `/api/chat`."""

    provider = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        max_tokens: int = 1024,
        timeout_seconds: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        #: `transport` is injected only by tests, which supply a `MockTransport`
        #: so the request/response contract can be asserted without a live Ollama.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "stream": False,
            "options": {
                # Deterministic-ish: this is a classifier, not a writer. Low
                # temperature also makes the structured output easier to hit.
                "temperature": 0,
                "num_predict": request.max_tokens or self._max_tokens,
            },
        }

        # Set by `MessageFilterAgent` for the classification call. Ollama
        # accepts a JSON schema here and constrains token sampling to it.
        schema = request.response_schema
        if schema is not None:
            payload["format"] = schema

        try:
            response = await self._client.post("/api/chat", json=payload)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

        text = str(body.get("message", {}).get("content", "")).strip()
        if not text:
            raise LLMError("Ollama returned an empty completion.")
        return LLMResponse(text=text, provider=self.provider, model=self.model)

    async def aclose(self) -> None:
        await self._client.aclose()
