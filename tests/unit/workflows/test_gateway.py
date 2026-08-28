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

    connect(
        FakeHandle(
            None, describe_raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        )
    )
    with pytest.raises(NotFoundError):
        await describe_ingestion_run("nope")
