"""Reading workflow state.

The gateway is thin, but it is where two vocabularies meet: Temporal's
execution statuses and the console's. A mapping gap here shows up as a run the
UI renders as permanently "queued".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from temporalio.client import WorkflowExecutionStatus

from app.core.errors import NotFoundError
from app.domains.identity.models import Platform
from app.workflows.dto import RunProgress, RunSummary
from app.workflows.gateway import STATUS, describe_ingestion_run


def summary(**overrides) -> RunSummary:  # type: ignore[no-untyped-def]
    base = {
        "run_id": "r1",
        "platform": Platform.SLACK,
        "started_at": datetime(2026, 8, 28, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 28, tzinfo=UTC),
    }
    return RunSummary(**{**base, **overrides})


class FakeHandle:
    """Stands in for an *untyped* handle, which is what the gateway gets.

    `result`/`query` here deliberately mimic the real client: they hand back
    plain JSON and only reconstruct a model when a `result_type` was named -
    on the handle for `result`, per call for `query`. A fake that always
    returned models would hide the bug this asserts against: the gateway
    forgetting to ask for one, and then reaching for attributes on a dict.
    """

    def __init__(self, status, *, result=None, progress=None, describe_raises=None):  # type: ignore[no-untyped-def]
        self._status = status
        self._result = result
        self._progress = progress or RunProgress()
        self._describe_raises = describe_raises
        self.result_type = None
        self.queried = False

    async def describe(self):  # type: ignore[no-untyped-def]
        if self._describe_raises:
            raise self._describe_raises
        return type("Description", (), {"status": self._status})()

    @staticmethod
    def _converted(value, result_type):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        payload = value.model_dump(mode="json")
        return result_type(**payload) if result_type is not None else payload

    async def result(self):  # type: ignore[no-untyped-def]
        return self._converted(self._result, self.result_type)

    async def query(self, name, *, result_type=None):  # type: ignore[no-untyped-def]
        self.queried = True
        return self._converted(self._progress, result_type)


class FakeClient:
    def __init__(self, handle):  # type: ignore[no-untyped-def]
        self.handle = handle

    def get_workflow_handle(self, workflow_id, *, result_type=None):  # type: ignore[no-untyped-def]
        # The real client attaches the type here; a handle fetched by id has
        # none otherwise, and `result()` then returns raw JSON.
        self.handle.result_type = result_type
        return self.handle


@pytest.fixture
def connect(monkeypatch):  # type: ignore[no-untyped-def]
    def install(handle):  # type: ignore[no-untyped-def]
        async def fake_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
            return FakeClient(handle)

        monkeypatch.setattr("app.workflows.gateway.client.connect", fake_connect)
        return handle

    return install


# -- status mapping ---------------------------------------------------------


def test_every_temporal_status_is_mapped() -> None:
    """An unmapped status silently degrades to 'queued', which would render a
    finished run as though it were still waiting."""
    unmapped = set(WorkflowExecutionStatus) - set(STATUS)
    assert not unmapped, f"unmapped Temporal statuses: {sorted(s.name for s in unmapped)}"


def test_terminal_failures_all_read_as_failed() -> None:
    """Terminated and timed-out are failures to an operator, whatever Temporal
    calls them internally."""
    for status in (
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
    ):
        assert STATUS[status] == "failed"


# -- describing a run -------------------------------------------------------


async def test_a_completed_run_carries_the_full_report(connect) -> None:
    connect(
        FakeHandle(
            WorkflowExecutionStatus.COMPLETED,
            result=summary(fetched=9, retained=4, persisted=4, embedded=4),
        )
    )
    view = await describe_ingestion_run("r1")

    assert view.status == "completed"
    assert view.progress.stage == "done"
    assert view.summary is not None
    assert view.summary.persisted == 4
    assert view.summary.platform is Platform.SLACK


async def test_a_running_run_reports_live_progress(connect) -> None:
    handle = connect(
        FakeHandle(
            WorkflowExecutionStatus.RUNNING,
            progress=RunProgress(stage="filtering", fetched=9, evaluated=9, filtered=5),
        )
    )
    view = await describe_ingestion_run("r1")

    assert handle.queried, "a running workflow should be queried for progress"
    assert view.status == "running"
    assert view.progress.stage == "filtering"
    assert view.progress.filtered == 5
    assert view.summary is None


async def test_a_failed_run_is_not_queried_for_progress(connect) -> None:
    """Querying a workflow that is no longer running just errors."""
    handle = connect(FakeHandle(WorkflowExecutionStatus.FAILED))
    view = await describe_ingestion_run("r1")

    assert view.status == "failed"
    assert handle.queried is False
    assert view.summary is None


async def test_a_progress_query_failure_does_not_fail_the_poll(connect, monkeypatch) -> None:
    """A momentarily unavailable worker must not turn a healthy run into an
    error the console renders as a failure."""
    handle = FakeHandle(WorkflowExecutionStatus.RUNNING)

    async def boom(name):  # type: ignore[no-untyped-def]
        raise RuntimeError("worker unavailable")

    handle.query = boom  # type: ignore[assignment]
    connect(handle)

    view = await describe_ingestion_run("r1")
    assert view.status == "running"
    assert view.progress.stage == "starting"


async def test_an_unknown_run_is_a_not_found(connect) -> None:
    from temporalio.service import RPCError, RPCStatusCode

    connect(FakeHandle(None, describe_raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")))
    with pytest.raises(NotFoundError):
        await describe_ingestion_run("nope")


# -- W2 / H2: run status outlives Temporal ----------------------------------
#
# A run's status used to exist only inside Temporal. Past the namespace's
# retention window - 24h by default on `temporalio/auto-setup:1.25.2` - a run
# that had completed perfectly well became a 404, with its full report sitting
# in `ingestion_runs` the whole time. The same 404 answered for a run the API
# had queued but never managed to start.


class FakeRunRow:
    """A row as `IngestionRunRepository.get_by_run_id` would return it."""

    def __init__(self, status: str, **counters) -> None:  # type: ignore[no-untyped-def]
        self.status = status
        self.platform = Platform.SLACK
        self.fetched = counters.get("fetched", 0)
        self.evaluated = counters.get("evaluated", 0)
        self.embedded = counters.get("embedded", 0)
        self.persisted = counters.get("persisted", 0)
        self.decisions = counters.get("decisions", [])


@pytest.fixture
def stored_run(monkeypatch):  # type: ignore[no-untyped-def]
    """Install a database answer for `_run_view_from_database` to find."""

    def install(row):  # type: ignore[no-untyped-def]
        async def fake_lookup(run_id: str):  # type: ignore[no-untyped-def]
            import uuid as _uuid

            try:
                _uuid.UUID(run_id)
            except ValueError:
                return None
            if row is None:
                return None
            from app.workflows.dto import RunProgress as _RunProgress
            from app.workflows.gateway import DB_STATUS
            from app.workflows.gateway import RunView as _RunView

            status = DB_STATUS.get(row.status, "failed")
            return _RunView(
                run_id=run_id,
                status=status,
                progress=_RunProgress(
                    platform=row.platform,
                    stage="done" if status == "completed" else status,
                    fetched=row.fetched,
                    evaluated=row.evaluated,
                    filtered=len(row.decisions),
                    embedded=row.embedded,
                    persisted=row.persisted,
                ),
            )

        monkeypatch.setattr("app.workflows.gateway._run_view_from_database", fake_lookup)

    return install


KNOWN_RUN = "3f6b1c22-0f1e-4a3d-9c77-2f9a5e4b1d80"


async def test_a_run_temporal_has_forgotten_is_served_from_the_database(
    connect, stored_run
) -> None:
    """Acceptance check W2 bullet 1."""
    from temporalio.service import RPCError, RPCStatusCode

    connect(FakeHandle(None, describe_raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")))
    stored_run(FakeRunRow("success", fetched=9, evaluated=9, persisted=4, embedded=4))

    view = await describe_ingestion_run(KNOWN_RUN)

    assert view.status == "completed"
    assert view.progress.persisted == 4
    assert view.progress.stage == "done"


async def test_a_queued_run_temporal_never_saw_is_reported_as_queued(connect, stored_run) -> None:
    """Acceptance check H2: the API wrote the row, `start_workflow` then failed.

    Reporting "queued" is the honest answer and, crucially, not a 404 - the run
    was genuinely requested.
    """
    from temporalio.service import RPCError, RPCStatusCode

    connect(FakeHandle(None, describe_raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")))
    stored_run(FakeRunRow("queued"))

    view = await describe_ingestion_run(KNOWN_RUN)

    assert view.status == "queued"


async def test_a_failed_run_survives_retention_as_failed(connect, stored_run) -> None:
    """The row the workflow's failure path wrote is what answers here."""
    from temporalio.service import RPCError, RPCStatusCode

    connect(FakeHandle(None, describe_raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")))
    stored_run(FakeRunRow("failed"))

    view = await describe_ingestion_run(KNOWN_RUN)

    assert view.status == "failed"


async def test_a_run_neither_temporal_nor_the_database_knows_is_still_a_404(
    connect, stored_run
) -> None:
    """The fallback must not turn every unknown id into a fabricated run."""
    from temporalio.service import RPCError, RPCStatusCode

    connect(FakeHandle(None, describe_raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")))
    stored_run(None)

    with pytest.raises(NotFoundError):
        await describe_ingestion_run(KNOWN_RUN)


async def test_temporal_still_wins_while_it_knows_the_run(connect, stored_run) -> None:
    """The database is a fallback, not a replacement: a live run's progress
    comes from the workflow query, which the database cannot answer."""
    handle = connect(
        FakeHandle(
            WorkflowExecutionStatus.RUNNING,
            progress=RunProgress(stage="embedding", embedded=7),
        )
    )
    stored_run(FakeRunRow("queued"))

    view = await describe_ingestion_run(KNOWN_RUN)

    assert handle.queried
    assert view.status == "running"
    assert view.progress.stage == "embedding"


# -- H5: the active-runs listing is cached ----------------------------------


async def test_the_active_runs_listing_is_cached_across_callers(monkeypatch) -> None:
    """N console tabs must cost one Temporal round trip per interval, not N.

    Each uncached call is a `list_workflows` plus a `progress` query per running
    run, against a worker that runs one activity at a time.
    """
    import asyncio

    from app.workflows import gateway

    gateway.reset_active_runs_cache()
    calls = {"n": 0}

    async def counted_fetch():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return []

    monkeypatch.setattr(gateway, "_fetch_active_runs", counted_fetch)

    await asyncio.gather(*(gateway.list_active_runs() for _ in range(20)))

    assert calls["n"] == 1, f"{calls['n']} fan-outs for 20 concurrent callers"
    gateway.reset_active_runs_cache()


async def test_the_active_runs_cache_expires(monkeypatch) -> None:
    """It is a 2-second cache, not a memo: the indicator has to stay live."""
    from app.workflows import gateway

    gateway.reset_active_runs_cache()
    calls = {"n": 0}

    async def counted_fetch():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return []

    monkeypatch.setattr(gateway, "_fetch_active_runs", counted_fetch)
    monkeypatch.setattr(
        gateway, "get_settings", lambda: type("S", (), {"temporal_active_runs_cache_seconds": 0})()
    )

    await gateway.list_active_runs()
    await gateway.list_active_runs()

    assert calls["n"] == 2
    gateway.reset_active_runs_cache()
