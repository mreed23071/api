"""The LLM port.

The two agentic steps in this service - filtering inbound messages and
summarizing a user's history - depend on this protocol, never on a vendor SDK.
That keeps the domain layer testable offline and makes the provider a
deployment decision rather than a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class LLMTask(StrEnum):
    """What the caller is asking for.

    Real providers ignore this; the offline stub uses it to pick a deterministic
    strategy, and it is a useful dimension for logging and metrics.
    """

    CLASSIFY = "classify"
    SUMMARIZE = "summarize"


@dataclass(slots=True, frozen=True)
class LLMRequest:
    system: str
    user: str
    task: LLMTask
    max_tokens: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    #: JSON schema the reply must conform to, when the caller needs structured
    #: output rather than prose. A *request*, not a guarantee: adapters whose
    #: provider cannot constrain decoding ignore it, so callers still have to
    #: parse defensively. Ollama enforces it during sampling, which is what
    #: makes a small local model usable for classification at all.
    response_schema: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str


class LLMError(RuntimeError):
    """Raised when the provider fails or returns something unusable."""


@runtime_checkable
class LLMClient(Protocol):
    """Port implemented by every provider adapter."""

    #: Adapter identity, surfaced in responses and audit logs.
    provider: str
    #: The concrete model (or heuristic version) answering requests.
    model: str

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Must raise `LLMError` on failure."""
        ...

    async def aclose(self) -> None:
        """Release any transport resources."""
        ...
