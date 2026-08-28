"""Transcript rendering - the part of summarization that determines quality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domains.insights.summarization import MAX_TRANSCRIPT_CHARS, SummarizationAgent
from app.shared.llm.base import LLMTask
from app.shared.llm.stub import StubLLMClient
from tests.factories import make_message, make_user
from tests.fakes import RecordingLLMClient


def agent(llm=None) -> SummarizationAgent:  # type: ignore[no-untyped-def]
    return SummarizationAgent(llm or StubLLMClient(), system_prompt="Summarise.")


def test_transcript_identifies_the_person() -> None:
    user = make_user(full_name="Alice Nguyen", email="alice@example.com")
    transcript = SummarizationAgent.render_transcript(user, [make_message(user)])
    assert "Alice Nguyen" in transcript and "alice@example.com" in transcript


def test_transcript_is_oldest_first() -> None:
    user = make_user()
    now = datetime.now(UTC)
    # Repositories return newest-first; the transcript should reverse that.
    messages = [
        make_message(user, content="newest", sent_at=now),
        make_message(user, content="oldest", sent_at=now - timedelta(days=1)),
    ]
    lines = SummarizationAgent.render_transcript(user, messages).splitlines()
    assert lines.index("[" + f"{messages[1].sent_at:%Y-%m-%d %H:%M}" + " slack] oldest") < lines.index(
        "[" + f"{messages[0].sent_at:%Y-%m-%d %H:%M}" + " slack] newest"
    )


def test_long_history_is_truncated_from_the_front() -> None:
    """Recency is what a summary is judged on, so the tail must survive."""
    user = make_user()
    messages = [make_message(user, content="x" * 500) for _ in range(50)]
    messages.append(make_message(user, content="THE-MOST-RECENT-LINE"))
    transcript = SummarizationAgent.render_transcript(user, list(reversed(messages)))

    assert len(transcript) < len("x" * 500) * 50
    assert "older messages truncated" in transcript
    assert "THE-MOST-RECENT-LINE" in transcript
    assert len(transcript) <= MAX_TRANSCRIPT_CHARS + 200


async def test_summarize_uses_the_summarize_task_and_configured_prompt() -> None:
    llm = RecordingLLMClient()
    user = make_user()
    await SummarizationAgent(llm, system_prompt="Be terse.").summarize(
        user, [make_message(user)]
    )
    assert llm.requests[0].task is LLMTask.SUMMARIZE
    assert llm.requests[0].system == "Be terse."
    assert llm.requests[0].metadata["user_id"] == str(user.id)
