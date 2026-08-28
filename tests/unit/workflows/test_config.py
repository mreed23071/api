"""Names and policies shared across process boundaries.

Small surface, but every value here is one that fails *silently* when it
disagrees between the API process and the worker: a mismatched task queue or
workflow type produces no error, just a workflow that waits forever.
"""

from __future__ import annotations

from app.workflows.config import (
    FETCH_RETRY,
    INFERENCE_RETRY,
    INFERENCE_TIMEOUT,
    INGESTION_TASK_QUEUE,
    INGESTION_WORKFLOW,
    WRITE_RETRY,
    run_workflow_id,
)
from app.workflows.ingestion import IngestionWorkflow


def test_the_workflow_name_matches_the_registered_workflow() -> None:
    """The API starts workflows by name; the worker registers the class.

    If these drift, `start_workflow` succeeds and nothing ever runs it.
    """
    assert IngestionWorkflow.__temporal_workflow_definition.name == INGESTION_WORKFLOW  # type: ignore[attr-defined]


def test_a_run_id_maps_to_exactly_one_workflow_id() -> None:
    assert run_workflow_id("abc") == run_workflow_id("abc")
    assert run_workflow_id("abc") != run_workflow_id("abd")


def test_the_workflow_id_carries_the_run_id() -> None:
    """It is the idempotency key: Temporal refuses a duplicate while running."""
    assert "abc" in run_workflow_id("abc")


def test_inference_backs_off_harder_than_fetching() -> None:
    """Fetching is a cheap idempotent read; inference is slow and paid for."""
    assert INFERENCE_RETRY.maximum_attempts < FETCH_RETRY.maximum_attempts
    assert INFERENCE_RETRY.maximum_interval > FETCH_RETRY.maximum_interval  # type: ignore[operator]


def test_writes_retry_generously_because_they_are_idempotent() -> None:
    """`ON CONFLICT DO NOTHING` means a repeated write cannot duplicate rows."""
    assert WRITE_RETRY.maximum_attempts >= FETCH_RETRY.maximum_attempts


def test_the_inference_timeout_exceeds_a_slow_local_batch() -> None:
    """A timeout here fails the batch closed, which discards messages - so it
    has to be longer than the slowest plausible run, not merely generous."""
    assert INFERENCE_TIMEOUT.total_seconds() >= 300


def test_the_task_queue_is_named() -> None:
    assert INGESTION_TASK_QUEUE
