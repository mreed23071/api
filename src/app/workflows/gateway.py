"""Starting runs and reading their state.

The client-side half of the workflow boundary, used by the API. It deals only
in `app.workflows.dto` types - mapping those onto the v1 wire contract is the
route's job, and doing it here would make the worker's package depend on the
HTTP layer (and, less abstractly, produce a circular import).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle
from temporalio.service import RPCError

from app.core.errors import NotFoundError
from app.domains.identity.models import Platform
from app.workflows import client
from app.workflows.config import INGESTION_TASK_QUEUE, INGESTION_WORKFLOW, run_workflow_id
from app.workflows.dto import IngestionInput, RunProgress, RunSummary

logger = logging.getLogger(__name__)

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
    """Hand the run to the worker and return immediately."""
    temporal = await client.connect()
    handle = await temporal.start_workflow(
        INGESTION_WORKFLOW,
        payload,
        id=run_workflow_id(payload.run_id),
        task_queue=INGESTION_TASK_QUEUE,
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


async def list_active_runs() -> list[ActiveRun]:
    """Every `IngestionWorkflow` currently in flight, across every platform -
    not history (which only gains a row once `record_run` fires at the very
    end), a live read of Temporal's own visibility store.
    """
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


async def describe_ingestion_run(run_id: str) -> RunView:
    """Current stage while running; the full report once finished."""
    temporal = await client.connect()
    # `result_type` is required, not optional politeness. This handle is
    # fetched by id rather than returned from `start_workflow`, so it carries
    # no type information; without it the converter hands back raw JSON and
    # the attribute access below fails on a dict.
    handle = temporal.get_workflow_handle(run_workflow_id(run_id), result_type=RunSummary)

    try:
        description = await handle.describe()
    except RPCError as exc:
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
