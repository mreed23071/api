"""Names and tuning shared by the client, the worker and the workflow.

In one module because a task queue name that disagrees between the process
starting a workflow and the process meant to run it produces no error - the
workflow simply waits forever. Same for the workflow type name.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

#: Every ingestion workflow and activity runs on this queue. The worker polls
#: it; the API starts workflows on it.
INGESTION_TASK_QUEUE = "ingestion"

#: Stable across deploys - it is how a running workflow is matched to its code.
INGESTION_WORKFLOW = "IngestionWorkflow"


def run_workflow_id(run_id: str) -> str:
    """One workflow per run, addressable by the id the API hands back.

    Temporal rejects a duplicate id while one is running, which makes this the
    idempotency key: the same run cannot be started twice.
    """
    return f"ingestion-{run_id}"


#: Fetching is a cheap, idempotent read. Retry it eagerly.
FETCH_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

#: Inference is slow and, against a hosted provider, paid for. Back off harder
#: and give up sooner - a batch that keeps failing should surface as a
#: fail-closed decision in the run report rather than retry indefinitely.
INFERENCE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
)

#: Writes are transactional and idempotent (`ON CONFLICT DO NOTHING` on
#: `(platform, external_message_id)`), so retrying is always safe.
WRITE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
)

#: A local 3B model on CPU is slow. This has to exceed the slowest plausible
#: batch, because a timeout here means the batch fails closed - messages get
#: discarded rather than retried.
INFERENCE_TIMEOUT = timedelta(minutes=10)
FETCH_TIMEOUT = timedelta(minutes=2)
WRITE_TIMEOUT = timedelta(minutes=2)
