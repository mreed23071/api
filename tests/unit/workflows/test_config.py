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


def test_inference_gives_up_sooner_than_fetching() -> None:
    """Fetching is a cheap idempotent read; inference is slow and paid for.

    "Backs off harder" used to mean a longer ceiling between attempts as well as
    fewer of them. The ceiling is no longer the lever: with one activity slot on
    the worker, a wedged batch blocks every run behind it for the whole of its
    timeout window, so what matters is the total - attempts times timeout - and
    that is bounded by taking attempts down, not by waiting longer between them.
    Both ceilings now sit at 30s; the attempt counts are what differ.
    """
    assert INFERENCE_RETRY.maximum_attempts < FETCH_RETRY.maximum_attempts
    assert INFERENCE_RETRY.backoff_coefficient >= FETCH_RETRY.backoff_coefficient  # type: ignore[operator]
    assert INFERENCE_RETRY.initial_interval > FETCH_RETRY.initial_interval  # type: ignore[operator]


def test_a_wedged_inference_batch_cannot_block_the_worker_for_half_an_hour() -> None:
    """The bound that matters on a single-slot worker.

    `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` is 1 by default, matching Ollama's own
    single-threaded configuration - so one batch that hangs until its timeout,
    repeatedly, stalls every other run for attempts x timeout. At three attempts
    of ten minutes that was about half an hour per batch.
    """
    attempts = INFERENCE_RETRY.maximum_attempts or 1
    worst_case = INFERENCE_TIMEOUT.total_seconds() * attempts
    assert worst_case <= 13 * 60


def test_writes_retry_generously_because_they_are_idempotent() -> None:
    """`ON CONFLICT DO NOTHING` means a repeated write cannot duplicate rows."""
    assert WRITE_RETRY.maximum_attempts >= FETCH_RETRY.maximum_attempts


def test_the_inference_timeout_exceeds_a_slow_local_batch() -> None:
    """A timeout here fails the batch closed, which discards messages - so it
    has to be longer than the slowest plausible run, not merely generous."""
    assert INFERENCE_TIMEOUT.total_seconds() >= 300


def test_the_task_queue_is_named() -> None:
    assert INGESTION_TASK_QUEUE
