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
from typing import Any

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
- Include EVERY message, including the ones you reject. A message you are
  dropping gets a decision with "keep": false - never omit it.
- "keep" must be a boolean.
- "category" must be one of: business, personal, automated, unclear.
- Keep "reason" under 140 characters.
""".strip()

#: The same contract as `RESPONSE_CONTRACT`, in a form a provider can enforce
#: during sampling. Passed on every classification request; adapters that
#: cannot constrain decoding ignore it and the prose contract above still
#: applies, so this is an upgrade rather than a dependency.
#:
#: This is what makes a small local model usable here. Asked politely, a 3B
#: model returns prose or a fenced block often enough that whole batches fail
#: closed - and a batch that fails closed silently *discards messages*. Given
#: the schema, the reply is valid JSON by construction.
def decision_schema(count: int) -> dict[str, Any]:
    """The response schema for a batch of exactly `count` messages.

    `minItems`/`maxItems` are the load-bearing part, and they are why this is a
    function rather than a constant. Given only the object shape, a 3B model
    returns valid JSON that omits every message it decided to *reject* - it
    expresses "drop" by leaving the entry out. Those then land as missing
    verdicts, which fail closed and are flagged `is_fallback`, so a run where
    the model worked correctly reports a pile of filter errors and the
    fail-closed signal stops meaning anything.

    Pinning the array to the batch length forces a decision for every message,
    rejections included.
    """
    return {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "keep": {"type": "boolean"},
                        "category": {
                            "type": "string",
                            "enum": ["business", "personal", "automated", "unclear"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "keep", "category", "reason"],
                },
            }
        },
        "required": ["decisions"],
    }

#: Small local models lose track of which id belongs to which verdict well
#: before they hit a context limit, and one bad batch costs every message in
#: it. Hosted frontier models do not have that problem and paying per call
#: makes large batches worth it, so the size is chosen per provider rather
#: than being one constant.
DEFAULT_BATCH_SIZE = 20
LOCAL_BATCH_SIZE = 5


def _batch_size_for(llm: LLMClient) -> int:
    """Pick a batch size from the provider's known reliability at this task."""
    return LOCAL_BATCH_SIZE if llm.provider == "ollama" else DEFAULT_BATCH_SIZE


class MessageFilterAgent:
    """Turns raw messages into keep/drop decisions."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        system_prompt: str,
        batch_size: int | None = None,
        fail_closed: bool = True,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        #: Defaults to a smaller batch for a locally-hosted model - see
        #: LOCAL_BATCH_SIZE. An explicit value always wins.
        self._batch_size = batch_size if batch_size is not None else _batch_size_for(llm)
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
            response_schema=decision_schema(len(batch)),
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
