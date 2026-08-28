"""LLM doubles: scripted, recording, and always-failing."""

from __future__ import annotations

from app.shared.llm.base import LLMError, LLMRequest, LLMResponse, LLMTask
from app.shared.llm.stub import StubLLMClient


class ScriptedLLMClient:
    """Returns a queued response per call; falls back to the heuristic stub.

    Use it to pin exactly what the model "said" for a test - including
    malformed output - without reaching for mocks.
    """

    provider = "scripted"
    model = "scripted-v1"

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self._fallback = StubLLMClient()
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._responses:
            return LLMResponse(
                text=self._responses.pop(0), provider=self.provider, model=self.model
            )
        return await self._fallback.complete(request)

    async def aclose(self) -> None:
        return None


class RecordingLLMClient:
    """Delegates to the stub while recording every request for assertions."""

    provider = "recording"
    model = "recording-v1"

    def __init__(self) -> None:
        self._inner = StubLLMClient()
        self.requests: list[LLMRequest] = []

    @property
    def tasks(self) -> list[LLMTask]:
        return [request.task for request in self.requests]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = await self._inner.complete(request)
        return LLMResponse(text=response.text, provider=self.provider, model=self.model)

    async def aclose(self) -> None:
        return None


class FailingLLMClient:
    """Always raises - exercises the fail-closed and degrade-gracefully paths."""

    provider = "failing"
    model = "failing-v1"

    def __init__(self, message: str = "provider is down") -> None:
        self._message = message
        self.call_count = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        raise LLMError(self._message)

    async def aclose(self) -> None:
        return None
