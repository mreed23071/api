"""The offline provider. It is the default, so its behaviour is a contract."""

from __future__ import annotations

import json

import pytest

from app.shared.llm.base import LLMError, LLMRequest, LLMTask
from app.shared.llm.stub import StubLLMClient


def classify_request(*messages: tuple[str, str]) -> LLMRequest:
    payload = {"messages": [{"id": mid, "text": text} for mid, text in messages]}
    return LLMRequest(system="policy", user=json.dumps(payload), task=LLMTask.CLASSIFY)


async def test_classification_returns_one_decision_per_message() -> None:
    response = await StubLLMClient().complete(
        classify_request(("m1", "deploy the release"), ("m2", "pizza on friday"))
    )
    decisions = json.loads(response.text)["decisions"]
    assert [d["id"] for d in decisions] == ["m1", "m2"]
    assert decisions[0]["keep"] is True
    assert decisions[1]["keep"] is False


async def test_classification_is_deterministic() -> None:
    request = classify_request(("m1", "sprint review agenda"))
    first = await StubLLMClient().complete(request)
    second = await StubLLMClient().complete(request)
    assert first.text == second.text


async def test_ambiguous_text_is_dropped_rather_than_kept() -> None:
    """The default provider inherits the fail-closed posture of the policy."""
    response = await StubLLMClient().complete(classify_request(("m1", "ok sounds good")))
    assert json.loads(response.text)["decisions"][0]["keep"] is False


async def test_classification_rejects_a_malformed_payload() -> None:
    with pytest.raises(LLMError):
        await StubLLMClient().complete(
            LLMRequest(system="policy", user="not json", task=LLMTask.CLASSIFY)
        )


async def test_summarization_mentions_message_count_and_recency() -> None:
    transcript = "# History\n[2026-01-01 09:00 slack] The deploy is blocked on review."
    response = await StubLLMClient().complete(
        LLMRequest(system="summarise", user=transcript, task=LLMTask.SUMMARIZE)
    )
    assert "1 retained message" in response.text
    assert "deploy" in response.text


async def test_summarizing_an_empty_history_says_so() -> None:
    response = await StubLLMClient().complete(
        LLMRequest(system="summarise", user="# History\n", task=LLMTask.SUMMARIZE)
    )
    assert "No messages" in response.text


def test_it_declares_provider_and_model_like_every_adapter() -> None:
    client = StubLLMClient()
    assert client.provider == "stub"
    assert client.model
