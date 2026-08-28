"""The filtering agent.

An LLM decides which inbound messages are worth retaining. The *policy* lives in
INGESTION_FILTER_SYSTEM_PROMPT (an operator can change "keep only business
messages" to "keep only messages about incidents" without a deploy); the code
here owns only the protocol - batching, the JSON contract, and what to do when
the model answers badly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from app.domains.ingestion.dto import FilterDecision, RawMessage
from app.shared.llm.base import LLMClient, LLMError, LLMRequest, LLMTask

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

RESPONSE_CONTRACT = """
You are given a JSON object with a "messages" array. Apply the policy above to
every message independently.

Reply with JSON only - no prose, no markdown fence - in exactly this shape:

{"decisions": [{"id": "<message id>", "keep": true, "category": "business", "reason": "<short justification>"}]}

Rules:
- Return exactly one decision per input message, using the same "id".
- "keep" must be a boolean.
- "category" must be one of: business, personal, automated, unclear.
- Keep "reason" under 140 characters.
""".strip()


class MessageFilterAgent:
    """Turns raw messages into keep/drop decisions."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        system_prompt: str,
        batch_size: int = 20,
        fail_closed: bool = True,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._batch_size = batch_size
        #: When the model misbehaves, drop the batch rather than store messages
        #: the policy may have wanted excluded. Privacy beats recall here.
        self._fail_closed = fail_closed

    @property
    def provider(self) -> str:
        """Which model backend is judging - recorded in every run report."""
        return self._llm.provider

    async def filter(self, messages: Sequence[RawMessage]) -> list[FilterDecision]:
        """Judge every message as business-relevant or personal.

        Works through the input in fixed-size batches rather than one call per
        message or one call for everything: per-message would be slow and
        expensive, and one enormous prompt degrades the model's accuracy and
        risks exceeding its context.

        Returns one decision per input message, in the same order.
        """
        decisions: list[FilterDecision] = []
        for start in range(0, len(messages), self._batch_size):
            batch = messages[start : start + self._batch_size]
            decisions.extend(await self._filter_batch(batch))
        return decisions

    async def _filter_batch(self, batch: Sequence[RawMessage]) -> list[FilterDecision]:
        """Judge one batch, failing closed if the model does not cooperate.

        If the call errors or the reply cannot be parsed, every message in the
        batch is rejected and marked `is_fallback`. That is the privacy-safe
        direction - discarding something that might have been retainable is
        recoverable, retaining something personal is not - and the flag is what
        keeps a provider outage distinguishable from a strict policy in the run
        report.
        """
        payload = {
            "messages": [
                {
                    "id": message.external_message_id,
                    "platform": message.platform.value,
                    "author": message.author_handle or message.external_author_id,
                    "text": message.content,
                }
                for message in batch
            ]
        }

        request = LLMRequest(
            system=f"{self._system_prompt}\n\n{RESPONSE_CONTRACT}",
            user=json.dumps(payload, ensure_ascii=False),
            task=LLMTask.CLASSIFY,
            metadata={"batch_size": str(len(batch))},
        )

        try:
            response = await self._llm.complete(request)
            parsed = self._parse(response.text)
        except (LLMError, ValueError) as exc:
            logger.error("Filtering agent failed for a batch of %d: %s", len(batch), exc)
            return [
                FilterDecision(
                    id=message.external_message_id,
                    keep=not self._fail_closed,
                    category="unclear",
                    reason=f"agent error: {exc}",
                    is_fallback=True,
                )
                for message in batch
            ]

        by_id = {decision.id: decision for decision in parsed}
        results: list[FilterDecision] = []
        for message in batch:
            decision = by_id.get(message.external_message_id)
            if decision is None:
                logger.warning(
                    "Filtering agent returned no verdict for %s", message.external_message_id
                )
                decision = FilterDecision(
                    id=message.external_message_id,
                    keep=not self._fail_closed,
                    category="unclear",
                    reason="no decision returned by the agent",
                    is_fallback=True,
                )
            results.append(decision)
        return results

    @staticmethod
    def _parse(text: str) -> list[FilterDecision]:
        """Be strict about the contract, forgiving about the wrapping."""
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            candidate = candidate.removeprefix("json").strip()
        match = _JSON_BLOCK_RE.search(candidate)
        if match is None:
            raise ValueError("no JSON object found in the agent response")

        document = json.loads(match.group(0))
        raw_decisions = document.get("decisions")
        if not isinstance(raw_decisions, list):
            raise ValueError("agent response is missing a 'decisions' array")
        return [FilterDecision.model_validate(item) for item in raw_decisions]
