"""The summarization agent.

One LLM call: a rendered transcript in, a few sentences out. Isolated from the
service so the prompt-shaping logic - which is what actually determines summary
quality - can be tested without a database.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domains.identity.models import User
from app.domains.messaging.models import Message
from app.shared.llm.base import LLMClient, LLMRequest, LLMTask

#: Ceiling on the transcript handed to the model. A real fix for long histories
#: is retrieval over the embeddings we already store (see S-1 in the prototype
#: report) rather than a larger constant.
MAX_TRANSCRIPT_CHARS = 8_000


class SummarizationAgent:
    """Turns a person's retained messages into a short narrative summary.

    A thin wrapper over the language model client: it owns the prompt and the
    transcript format, and nothing else. Keeping it separate from the service
    means the prompt can change without touching how summaries are scheduled,
    bounded or stored.
    """

    def __init__(self, llm: LLMClient, *, system_prompt: str, max_tokens: int = 512) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens

    @property
    def provider(self) -> str:
        """Which model backend generated a summary - recorded alongside it."""
        return self._llm.provider

    @property
    def model(self) -> str:
        """The exact model name, so a summary stays interpretable later."""
        return self._llm.model

    async def summarize(self, user: User, messages: Sequence[Message]) -> str:
        """Generate one person's summary from their messages.

        Raises `LLMError` if the provider fails. Callers are expected to catch
        it and degrade that person's entry rather than the whole request.
        """
        response = await self._llm.complete(
            LLMRequest(
                system=self._system_prompt,
                user=self.render_transcript(user, messages),
                task=LLMTask.SUMMARIZE,
                max_tokens=self._max_tokens,
                metadata={"user_id": str(user.id)},
            )
        )
        return response.text.strip()

    @staticmethod
    def render_transcript(user: User, messages: Sequence[Message]) -> str:
        """Format messages into the transcript the model reads.

        Oldest first, because a conversation reads more naturally forwards. If
        the result exceeds the character cap, the *start* is dropped rather than
        the end: a summary is judged on how well it describes recent behaviour,
        so the recent tail is the part worth keeping.
        """
        header = f"# Communication history for {user.full_name} <{user.email}>"
        lines = [
            f"[{message.sent_at:%Y-%m-%d %H:%M} {message.platform.value}] {message.content}"
            for message in reversed(messages)  # oldest first reads more naturally
        ]
        body = "\n".join(lines)
        if len(body) > MAX_TRANSCRIPT_CHARS:
            # Keep the most recent tail: recency is what a summary is judged on.
            body = "...[older messages truncated]...\n" + body[-MAX_TRANSCRIPT_CHARS:]
        return f"{header}\n\n{body}"
