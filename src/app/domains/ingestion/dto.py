"""Internal contracts published by the ingestion context."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.domains.identity.dto import IdentityCandidate
from app.domains.identity.models import Platform


class RawMessage(BaseModel):
    """A message exactly as an upstream connector hands it to us.

    The anti-corruption boundary: connectors normalise into this shape, and
    nothing downstream needs to know where a message came from.
    """

    model_config = ConfigDict(frozen=True)

    external_message_id: str
    platform: Platform
    external_author_id: str
    author_handle: str | None = None
    author_email: str | None = None
    author_display_name: str | None = None
    conversation_id: str | None = None
    content: str
    sent_at: datetime
    metadata: dict = Field(default_factory=dict)

    @property
    def key(self) -> tuple[Platform, str]:
        """The pair that identifies this message uniquely across all platforms.

        Matches the `(platform, external_message_id)` unique constraint on the
        table, which is what makes re-running ingestion safe: a message already
        stored is recognised and skipped rather than duplicated.
        """
        return (self.platform, self.external_message_id)

    def as_identity_candidate(self) -> IdentityCandidate:
        """Extract just the author, for the identity resolution step.

        A raw message carries both content and authorship. Identity resolution
        only needs the second, and handing it the whole message would let a
        context that should know nothing about message bodies see them.
        """
        return IdentityCandidate(
            platform=self.platform,
            external_id=self.external_author_id,
            handle=self.author_handle,
            email=self.author_email,
            display_name=self.author_display_name,
        )


class FilterDecision(BaseModel):
    """One verdict from the filtering agent."""

    id: str
    keep: bool
    category: str = "unknown"
    reason: str | None = None
    #: True when this verdict is a fail-closed default rather than a real
    #: judgement, so a provider outage is visible in the run report instead of
    #: being indistinguishable from a policy rejection.
    is_fallback: bool = False


@dataclass(frozen=True, slots=True)
class IngestionOptions:
    """Per-run overrides supplied by the caller."""

    limit: int | None = None
    system_prompt_override: str | None = None
    dry_run: bool = False


@dataclass(slots=True)
class IngestionRunResult:
    """What one pipeline execution did. The API maps this to a wire schema."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    dry_run: bool

    fetched: int = 0
    already_ingested: int = 0
    evaluated: int = 0
    retained: int = 0
    discarded: int = 0
    filter_errors: int = 0
    embedded: int = 0
    persisted: int = 0
    users_provisioned: int = 0
    relations_provisioned: int = 0

    filter_provider: str = "unknown"
    filter_prompt_version: str = "unknown"
    embedding_model: str = "unknown"
    decisions: list[FilterDecision] = field(default_factory=list)


class ConnectorStatus(StrEnum):
    """How healthy a platform integration looks, judged only by what arrived.

    Nothing here polls a connector or checks a credential - no real connector
    exists yet. These values are inferred from the data already stored, and the
    distinction matters: a platform can look CONNECTED here while its API token
    expired an hour ago. When real connectors land, this becomes a reported
    status and these rules go away.
    """

    #: Accounts exist and something arrived recently.
    CONNECTED = "connected"
    #: Accounts exist, but nothing has arrived in a while.
    DEGRADED = "degraded"
    #: Accounts exist and nothing has arrived in a long time.
    NEEDS_ATTENTION = "needs_attention"
    #: No accounts on this platform at all.
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """One platform's contribution, as the integrations screen shows it."""

    platform: Platform
    status: ConnectorStatus
    last_sync_at: datetime | None
    messages_contributed: int
    account_count: int
