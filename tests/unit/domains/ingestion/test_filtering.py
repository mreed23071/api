"""The filtering agent's protocol handling.

What is under test is the *contract* with the model - batching, parsing, and
what happens when the model misbehaves - not the model's judgement.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import DEFAULT_FILTER_PROMPT
from app.domains.ingestion.filtering import MessageFilterAgent
from app.domains.ingestion.sources import MockMessageService
from app.shared.llm.stub import StubLLMClient
from tests.factories import make_raw_message
from tests.fakes import FailingLLMClient, RecordingLLMClient, ScriptedLLMClient


def agent(llm=None) -> MessageFilterAgent:  # type: ignore[no-untyped-def]
    return MessageFilterAgent(llm or StubLLMClient(), system_prompt=DEFAULT_FILTER_PROMPT)


async def test_business_retained_and_personal_dropped() -> None:
    decisions = await agent().filter(
        [
            make_raw_message(
                external_message_id="m1",
                content="The production deploy is blocked on the schema migration review.",
            ),
            make_raw_message(
                external_message_id="m2", content="Anyone up for beers and pizza this weekend?"
            ),
        ]
    )
    assert {d.id: d.keep for d in decisions} == {"m1": True, "m2": False}


async def test_every_message_receives_exactly_one_decision_in_order() -> None:
    messages = list(await MockMessageService().fetch())
    decisions = await agent().filter(messages)
    assert [d.id for d in decisions] == [m.external_message_id for m in messages]


async def test_batching_splits_large_inputs() -> None:
    llm = RecordingLLMClient()
    messages = [make_raw_message(external_message_id=f"m{i}") for i in range(45)]
    decisions = await MessageFilterAgent(
        llm, system_prompt=DEFAULT_FILTER_PROMPT, batch_size=20
    ).filter(messages)

    assert len(decisions) == 45
    assert len(llm.requests) == 3  # 20 + 20 + 5


async def test_the_configured_policy_reaches_the_model() -> None:
    llm = RecordingLLMClient()
    await MessageFilterAgent(llm, system_prompt="Keep only incident reports.").filter(
        [make_raw_message()]
    )
    assert llm.requests[0].system.startswith("Keep only incident reports.")


async def test_agent_failure_fails_closed_and_is_flagged() -> None:
    """A provider outage must drop messages, and must be visible in the report."""
    decisions = await agent(FailingLLMClient()).filter([make_raw_message(external_message_id="m1")])
    assert decisions[0].keep is False
    assert decisions[0].is_fallback is True
    assert "agent error" in (decisions[0].reason or "")


async def test_unparseable_response_fails_closed() -> None:
    decisions = await agent(ScriptedLLMClient(["I'm afraid I can't do that."])).filter(
        [make_raw_message(external_message_id="m1")]
    )
    assert decisions[0].keep is False
    assert decisions[0].is_fallback is True


async def test_missing_verdict_for_a_message_fails_closed() -> None:
    """The model answered, but skipped one of the messages we sent."""
    response = json.dumps({"decisions": [{"id": "m1", "keep": True, "category": "business"}]})
    decisions = await agent(ScriptedLLMClient([response])).filter(
        [
            make_raw_message(external_message_id="m1"),
            make_raw_message(external_message_id="m2"),
        ]
    )
    verdicts = {d.id: d for d in decisions}
    assert verdicts["m1"].keep is True and verdicts["m1"].is_fallback is False
    assert verdicts["m2"].keep is False and verdicts["m2"].is_fallback is True


@pytest.mark.parametrize(
    "wrapped",
    [
        '```json\n{"decisions": [{"id": "m1", "keep": true, "category": "business"}]}\n```',
        'Sure!\n{"decisions": [{"id": "m1", "keep": true, "category": "business"}]}',
        '{"decisions": [{"id": "m1", "keep": true, "category": "business"}]}\n',
    ],
)
def test_parser_tolerates_chatty_wrapping(wrapped: str) -> None:
    decisions = MessageFilterAgent._parse(wrapped)
    assert decisions[0].id == "m1" and decisions[0].keep is True


def test_parser_rejects_a_response_with_no_decisions_array() -> None:
    with pytest.raises(ValueError, match="decisions"):
        MessageFilterAgent._parse('{"result": "ok"}')


async def test_empty_input_makes_no_model_call() -> None:
    llm = RecordingLLMClient()
    assert await agent(llm).filter([]) == []
    assert llm.requests == []
