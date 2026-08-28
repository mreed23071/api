"""Connector health, and the run history the console lists.

The health values are *inferred* from stored data - nothing polls a connector,
because no real connector exists yet. These tests pin the inference rules so
that when real reporting arrives, the change is visible rather than silent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.security.principal import Principal
from app.domains.identity.models import Platform
from app.domains.ingestion.dto import (
    ConnectorStatus,
    IngestionOptions,
    IngestionRunResult,
)
from app.domains.ingestion.service import _health_of, _status_of
from tests.factories import make_message, make_relation, make_user
from tests.fakes.uow import FakeUnitOfWork
from tests.unit.domains.ingestion.test_service import build

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def result(**overrides) -> IngestionRunResult:  # type: ignore[no-untyped-def]
    defaults = {
        "run_id": "run-1",
        "started_at": NOW,
        "finished_at": NOW,
        "duration_ms": 10,
        "dry_run": False,
    }
    return IngestionRunResult(**{**defaults, **overrides})


# -- the inference rules, in isolation ------------------------------------


def test_a_platform_with_no_accounts_is_disconnected() -> None:
    assert _health_of(0, None, NOW) is ConnectorStatus.DISCONNECTED


def test_accounts_but_no_traffic_ever_needs_attention() -> None:
    assert _health_of(3, None, NOW) is ConnectorStatus.NEEDS_ATTENTION


def test_recent_traffic_is_connected() -> None:
    assert _health_of(3, NOW - timedelta(days=1), NOW) is ConnectorStatus.CONNECTED


def test_traffic_within_the_month_is_degraded() -> None:
    assert _health_of(3, NOW - timedelta(days=10), NOW) is ConnectorStatus.DEGRADED


def test_stale_traffic_needs_attention() -> None:
    assert _health_of(3, NOW - timedelta(days=90), NOW) is ConnectorStatus.NEEDS_ATTENTION


def test_a_clean_run_is_a_success() -> None:
    assert _status_of(result(retained=5, persisted=5)) == "success"


def test_a_run_with_filter_fallbacks_is_partial() -> None:
    """Fail-closed defaults mean the policy was not really applied."""
    assert _status_of(result(retained=5, persisted=5, filter_errors=2)) == "partial"


def test_a_run_that_kept_nothing_it_meant_to_keep_failed() -> None:
    assert _status_of(result(retained=5, persisted=0, filter_errors=5)) == "failed"


# -- the assembled view ----------------------------------------------------


async def test_every_platform_appears_even_with_nothing_on_it() -> None:
    """A platform missing from the screen looks the same as one that works."""
    svc = build(FakeUnitOfWork(), principal=Principal.anonymous())

    connectors = await svc.connectors()

    assert {c.platform for c in connectors} == set(Platform)
    assert all(c.status is ConnectorStatus.DISCONNECTED for c in connectors)


async def test_a_platform_reports_its_accounts_and_message_volume() -> None:
    amara = make_user()
    slack = make_relation(amara, platform=Platform.SLACK)
    uow = FakeUnitOfWork(
        users=[amara],
        relations=[slack],
        messages=[
            make_message(amara, platform=Platform.SLACK, sent_at=NOW - timedelta(days=1)),
            make_message(amara, platform=Platform.SLACK, sent_at=NOW - timedelta(days=2)),
        ],
    )
    svc = build(uow, principal=Principal.anonymous())

    slack_health = next(c for c in await svc.connectors() if c.platform is Platform.SLACK)

    assert slack_health.account_count == 1
    assert slack_health.messages_contributed == 2
    assert slack_health.last_sync_at == NOW - timedelta(days=1)


# -- history ---------------------------------------------------------------


async def test_a_completed_run_is_recorded_with_its_decisions() -> None:
    uow = FakeUnitOfWork()
    svc = build(uow)

    await svc.run(IngestionOptions())

    assert len(uow.runs.runs) == 1
    assert uow.runs.runs[0].decisions, "the filtering verdicts are the audit trail"


async def test_a_dry_run_is_still_recorded() -> None:
    """The run whose record matters most is the one testing a policy change."""
    uow = FakeUnitOfWork()
    svc = build(uow)

    await svc.run(IngestionOptions(dry_run=True))

    assert len(uow.runs.runs) == 1
    assert uow.runs.runs[0].dry_run is True
    assert uow.runs.runs[0].persisted == 0
