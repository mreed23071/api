"""Offline, deterministic LLM adapter.

This is the default provider so the whole stack - compose up, migrate, ingest,
summarize - runs end to end with no credentials and no egress. It is a
heuristic, not a model: it exists to keep the pipeline honest in CI and local
development, and is swapped for `anthropic` by setting LLM_PROVIDER.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from app.shared.llm.base import LLMError, LLMRequest, LLMResponse, LLMTask

BUSINESS_MARKERS = frozenset(
    {
        "deploy", "deployment", "release", "sprint", "standup", "ticket", "jira",
        "pr", "review", "merge", "rollback", "incident", "outage", "postmortem",
        "roadmap", "milestone", "deadline", "client", "customer", "invoice",
        "contract", "budget", "meeting", "agenda", "spec", "requirements",
        "api", "schema", "migration", "staging", "production", "backlog",
        "estimate", "onboarding", "handover", "escalation", "sla", "kpi",
    }
)

PERSONAL_MARKERS = frozenset(
    {
        "birthday", "wedding", "vacation", "holiday", "beer", "pizza", "lunch",
        "dinner", "party", "weekend", "gym", "netflix", "dog", "cat", "baby",
        "hangover", "concert", "date", "girlfriend", "boyfriend", "husband",
        "wife", "sick", "doctor", "dentist", "meme", "lol", "haha",
    }
)

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


class StubLLMClient:
    """Deterministic stand-in for a hosted model."""

    provider = "stub"

    def __init__(self, model: str = "stub-heuristic-v1") -> None:
        self.model = model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        match request.task:
            case LLMTask.CLASSIFY:
                text = self._classify(request.user)
            case LLMTask.SUMMARIZE:
                text = self._summarize(request.user)
            case _:  # pragma: no cover - StrEnum is exhaustive today
                raise LLMError(f"Stub provider cannot handle task {request.task!r}")
        return LLMResponse(text=text, provider=self.provider, model=self.model)

    async def aclose(self) -> None:
        return None

    # -- strategies --------------------------------------------------------

    def _classify(self, payload: str) -> str:
        """Mirror the JSON contract the real filtering agent expects back."""
        try:
            messages = json.loads(payload)["messages"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LLMError(f"Stub classifier received an unparseable payload: {exc}") from exc

        decisions = []
        for message in messages:
            tokens = set(_tokens(str(message.get("text", ""))))
            business_hits = tokens & BUSINESS_MARKERS
            personal_hits = tokens & PERSONAL_MARKERS
            keep = len(business_hits) > len(personal_hits)
            decisions.append(
                {
                    "id": message.get("id"),
                    "keep": keep,
                    "category": "business" if keep else "personal",
                    "reason": (
                        f"matched business markers {sorted(business_hits)}"
                        if keep
                        else f"matched personal markers {sorted(personal_hits) or ['none']}"
                    ),
                }
            )
        return json.dumps({"decisions": decisions})

    def _summarize(self, transcript: str) -> str:
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        bodies = [line.split("] ", 1)[-1] for line in lines if not line.startswith("#")]
        if not bodies:
            return "No messages on record for this user."

        counter = Counter(
            token
            for body in bodies
            for token in _tokens(body)
            if token in BUSINESS_MARKERS
        )
        topics = ", ".join(topic for topic, _ in counter.most_common(4)) or "general updates"
        return (
            f"Communicated {len(bodies)} retained message(s), most often about {topics}. "
            f"Most recent message: \"{bodies[-1][:160]}\"."
        )
