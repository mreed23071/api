"""Starting runs and reading their state.

The client-side half of the workflow boundary, used by the API. It deals only
in `app.workflows.dto` types - mapping those onto the v1 wire contract is the
route's job, and doing it here would make the worker's package depend on the
HTTP layer (and, less abstractly, produce a circular import).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError

from app.core.config import get_settings
from app.core.db.engine import get_sessionmaker
from app.core.errors import NotFoundError
from app.core.security.principal import TenantContext
from app.domains.identity.models import Platform
from app.domains.ingestion.models import IngestionRun
from app.domains.uow import UnitOfWork
from app.workflows import client
from app.workflows.config import INGESTION_TASK_QUEUE, INGESTION_WORKFLOW, run_workflow_id
from app.workflows.dto import IngestionInput, RunProgress, RunSummary

logger = logging.getLogger(__name__)

#: How a terminal database row maps onto the vocabulary the console speaks.
#: `_status_of` writes the first three; the API and the workflow's failure path
#: write the last two.
DB_STATUS = {
    "success": "completed",
    "partial": "completed",
    "failed": "failed",
    "queued": "queued",
    "running": "running",
}

#: Temporal's execution statuses, in the vocabulary the console speaks.
STATUS = {
    WorkflowExecutionStatus.RUNNING: "running",
    WorkflowExecutionStatus.COMPLETED: "completed",
    WorkflowExecutionStatus.FAILED: "failed",
    WorkflowExecutionStatus.CANCELED: "cancelled",
    WorkflowExecutionStatus.TERMINATED: "failed",
    WorkflowExecutionStatus.TIMED_OUT: "failed",
    WorkflowExecutionStatus.CONTINUED_AS_NEW: "running",
}


class RunView(BaseModel):
    """A run's current state: live counters, plus the report once it finishes."""

    run_id: str
    status: str
    progress: RunProgress = RunProgress()
    #: Populated only when `status == "completed"`.
    summary: RunSummary | None = None


async def start_ingestion_run(payload: IngestionInput) -> WorkflowHandle[Any, Any]:
    """Hand the run to the worker and return immediately.

    Bounded, deliberately. An unbounded workflow queued while no worker is
    polling stays in flight forever: nothing ever fails it, so the console's
    active list never clears and the status endpoint reports "starting" for as
    long as the namespace retains it. Both timeouts are set because they answer
    different questions - `execution_timeout` bounds the whole execution
    including retries, `run_timeout` bounds a single run of it.
    """
    settings = get_settings()
    timeout = timedelta(seconds=settings.temporal_run_timeout_seconds)
    temporal = await client.connect()
    handle = await temporal.start_workflow(
        INGESTION_WORKFLOW,
        payload,
        # Unchanged, byte for byte: the route documents that the workflow id
        # derived from `run_id` is what stops the same run being started twice.
        id=run_workflow_id(payload.run_id),
        task_queue=INGESTION_TASK_QUEUE,
        execution_timeout=timeout,
        run_timeout=timeout,
        # `run_id` is a fresh uuid4 per submission, so this never rejects a
        # legitimate request - it turns a genuine double-submit of the same id
        # into an error instead of silently starting a second execution.
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    logger.info(
        "queued ingestion run",
        extra={
            "run_id": payload.run_id,
            "platform": payload.platform.value,
            "workflow_id": handle.id,
        },
    )
    return handle


class ActiveRun(BaseModel):
    """One in-flight run, as much as is cheap to know about it live.

    `platform` and `stage` come from the workflow's own `progress` query
    rather than a Temporal search attribute - registering one would be real
    setup for information only this listing reads. A query that fails (the
    worker is momentarily unavailable) degrades to `None`/`"starting"` rather
    than dropping the run from the list; it is still genuinely running.
    """

    run_id: str
    platform: Platform | None = None
    stage: str = "starting"
    started_at: datetime


#: A very short-lived cache over `list_active_runs`, and the lock that keeps
#: concurrent callers from all missing it at once. Process-local and
#: deliberately tiny: this is a console-wide indicator polled from every open
#: tab, and the uncached call fans out into one `list_workflows` plus a
#: `progress` query per running run. Without this, N tabs cost N fan-outs every
#: poll interval against a worker configured to run exactly one activity at a
#: time. Whether those queries genuinely contend for workflow-task slots is
#: unconfirmed; collapsing them is correct either way.
_active_cache: tuple[float, list[ActiveRun]] | None = None
_active_lock = asyncio.Lock()


def reset_active_runs_cache() -> None:
    """Drop the cached active-run listing. Test-support only."""
    global _active_cache
    _active_cache = None


async def list_active_runs() -> list[ActiveRun]:
    """Every `IngestionWorkflow` currently in flight, across every platform -
    not history (which only gains a row once `record_run` fires at the very
    end), a live read of Temporal's own visibility store.

    Answers from a short TTL cache; see `_active_cache`.
    """
    ttl = get_settings().temporal_active_runs_cache_seconds
    now = time.monotonic()

    cached = _active_cache
    if cached is not None and now - cached[0] < ttl:
        return cached[1]

    async with _active_lock:
        # Re-check under the lock: while this caller waited, another may have
        # already refreshed it, and the point is one round trip per interval.
        cached = _active_cache
        now = time.monotonic()
        if cached is not None and now - cached[0] < ttl:
            return cached[1]

        active = await _fetch_active_runs()
        _store_active_runs(active)
        return active


def _store_active_runs(active: list[ActiveRun]) -> None:
    global _active_cache
    _active_cache = (time.monotonic(), active)


async def _fetch_active_runs() -> list[ActiveRun]:
    temporal = await client.connect()
    active: list[ActiveRun] = []
    async for execution in temporal.list_workflows(
        query="WorkflowType='IngestionWorkflow' AND ExecutionStatus='Running'"
    ):
        # Strip the `ingestion-` prefix `run_workflow_id` adds, so this
        # matches the bare run_id the rest of the API speaks in.
        run_id = execution.id.removeprefix("ingestion-")
        progress = RunProgress()
        try:
            handle = temporal.get_workflow_handle(execution.id)
            progress = await handle.query("progress", result_type=RunProgress)
        except Exception:  # pragma: no cover - transient worker unavailability
            logger.warning("progress query failed for run %s", run_id)
        active.append(
            ActiveRun(
                run_id=run_id,
                platform=progress.platform,
                stage=progress.stage,
                started_at=execution.start_time,
            )
        )
    return active


async def _run_view_from_database(run_id: str) -> RunView | None:
    """Reconstruct a run's state from `ingestion_runs`, or `None` if unknown.

    The fallback that makes a run's status outlive Temporal. Two cases need it:

    * **Retention.** `temporalio/auto-setup:1.25.2` ships a default namespace
      retention of 24 hours [VERIFY: confirm against the deployed cluster with
      `temporal operator namespace describe default`; the documented default for
      this image is 24h, and a real deployment usually raises it]. Past that,
      Temporal has genuinely forgotten the execution and `describe()` is a
      not-found - for a run that completed perfectly well and whose report is
      sitting in the database.
    * **Runs Temporal never saw.** A `"queued"` row written by the API when
      `start_workflow` then failed, or a `"failed"` row from a workflow whose
      history has since aged out.

    No live progress here by construction: a terminal row has counters, not a
    stage. That is the honest answer for a run that is over.
    """
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        # Not a run id this system ever minted, so nothing to look up.
        return None

    async with get_sessionmaker()() as session:
        uow = UnitOfWork(session, TenantContext.global_scope())
        run: IngestionRun | None = await uow.runs.get_by_run_id(parsed)

    if run is None:
        return None

    status = DB_STATUS.get(run.status, "failed")
    return RunView(
        run_id=run_id,
        status=status,
        progress=RunProgress(
            platform=run.platform,
            stage="done" if status == "completed" else status,
            fetched=run.fetched,
            evaluated=run.evaluated,
            filtered=len(run.decisions),
            embedded=run.embedded,
            persisted=run.persisted,
        ),
    )


async def describe_ingestion_run(run_id: str) -> RunView:
    """Current stage while running; the full report once finished.

    Falls back to the database when Temporal has no record - see
    `_run_view_from_database`.
    """
    temporal = await client.connect()
    # `result_type` is required, not optional politeness. This handle is
    # fetched by id rather than returned from `start_workflow`, so it carries
    # no type information; without it the converter hands back raw JSON and
    # the attribute access below fails on a dict.
    handle = temporal.get_workflow_handle(run_workflow_id(run_id), result_type=RunSummary)

    try:
        description = await handle.describe()
    except RPCError as exc:
        # Temporal does not know this execution. Before calling it unknown, ask
        # the database - it outlives namespace retention, and it holds the rows
        # for runs Temporal never saw at all.
        view = await _run_view_from_database(run_id)
        if view is not None:
            return view
        raise NotFoundError(
            f"No ingestion run with id '{run_id}'.", details={"run_id": run_id}
        ) from exc

    status = STATUS.get(description.status, "queued") if description.status else "queued"

    if status == "completed":
        summary = await handle.result()
        return RunView(
            run_id=run_id,
            status=status,
            progress=RunProgress(
                stage="done",
                fetched=summary.fetched,
                evaluated=summary.evaluated,
                filtered=len(summary.decisions),
                embedded=summary.embedded,
                persisted=summary.persisted,
            ),
            summary=summary,
        )

    progress = RunProgress()
    if status == "running":
        # A query reaches the running workflow directly. It can fail if the
        # worker is momentarily unavailable, and a missing progress report
        # should not turn a healthy run into an error the console shows as a
        # failure.
        try:
            progress = await handle.query("progress", result_type=RunProgress)
        except Exception:  # pragma: no cover - transient worker unavailability
            logger.warning("progress query failed for run %s", run_id)

    return RunView(run_id=run_id, status=status, progress=progress)
