"""The Ollama chat adapter's contract with the wire.

The important assertion here is `response_schema` -> `format`. Without it a 3B
model answers the filter with prose often enough that whole batches fail closed
and messages are silently discarded, so that wiring is load-bearing rather than
an optimisation.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.shared.llm.base import LLMClient, LLMError, LLMRequest, LLMTask
from app.shared.llm.ollama_client import OllamaLLMClient

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def build(handler) -> OllamaLLMClient:  # type: ignore[no-untyped-def]
    return OllamaLLMClient(
        base_url="http://ollama:11434",
        model="llama3.2:3b",
        transport=httpx.MockTransport(handler),
    )


def reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


def request(**overrides) -> LLMRequest:  # type: ignore[no-untyped-def]
    fields: dict = {"system": "policy", "user": "payload", "task": LLMTask.CLASSIFY}
    return LLMRequest(**{**fields, **overrides})


def test_it_satisfies_the_port() -> None:
    assert isinstance(build(lambda r: reply("x")), LLMClient)


async def test_system_and_user_become_chat_messages() -> None:
    seen: dict = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(json.loads(r.content))
        seen["url"] = str(r.url)
        return reply("answer")

    response = await build(handler).complete(request())

    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["messages"] == [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "payload"},
    ]
    assert seen["stream"] is False
    assert response.text == "answer"
    assert response.provider == "ollama"


async def test_a_response_schema_is_sent_as_ollamas_format() -> None:
    """Constrained decoding is what makes a small local model usable here."""
    seen: dict = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(json.loads(r.content))
        return reply("{}")

    await build(handler).complete(request(response_schema=SCHEMA))
    assert seen["format"] == SCHEMA


async def test_no_schema_means_no_format_key() -> None:
    """Summarization wants prose; sending a schema would mangle it."""
    seen: dict = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(json.loads(r.content))
        return reply("a summary")

    await build(handler).complete(request(task=LLMTask.SUMMARIZE))
    assert "format" not in seen


async def test_classification_is_sampled_deterministically() -> None:
    """Temperature 0: the same batch should not get different verdicts on a retry."""
    seen: dict = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(json.loads(r.content))
        return reply("{}")

    await build(handler).complete(request())
    assert seen["options"]["temperature"] == 0


async def test_max_tokens_is_forwarded() -> None:
    seen: dict = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(json.loads(r.content))
        return reply("x")

    await build(handler).complete(request(max_tokens=64))
    assert seen["options"]["num_predict"] == 64


async def test_an_empty_completion_is_an_error() -> None:
    """Silently returning "" would be parsed as a failed batch and fail closed."""
    with pytest.raises(LLMError, match="empty completion"):
        await build(lambda r: reply("   ")).complete(request())


async def test_a_transport_failure_becomes_an_llm_error() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMError, match="Ollama request failed"):
        await build(handler).complete(request())


async def test_an_http_error_becomes_an_llm_error() -> None:
    with pytest.raises(LLMError, match="Ollama request failed"):
        await build(lambda r: httpx.Response(500, text="boom")).complete(request())
