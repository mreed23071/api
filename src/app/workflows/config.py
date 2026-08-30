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
#:
#: Two attempts, not three, and a shorter ceiling between them. Combined with
#: the six-minute `INFERENCE_TIMEOUT` below, the worst case for one wedged batch
#: drops from roughly thirty minutes to about twelve and a half - and with the
#: single-slot worker, a wedged batch blocks every other run behind it for that
#: whole time. A third attempt against a model that has already failed twice
#: buys very little for the twelve minutes it costs.
INFERENCE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=2,
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
#:
#: Six minutes, down from ten. The real bound on a hung call is not this
#: timeout: it is the httpx timeout inside the Ollama adapter, set from
#: `OLLAMA_TIMEOUT_SECONDS` (300s by default, verified applied in both
#: `shared/llm/ollama_client.py` and `shared/embeddings/ollama.py`). A call that
#: is going to fail has already failed by ~310s, so everything past that is dead
#: air during which the single-slot worker runs nothing else.
#:
#: Note what the heartbeat does and does not prove. `_with_heartbeat` beats
#: every 5s from a *separate task*, so it proves the worker process is alive -
#: not that the model is making progress. A model wedged mid-generation
#: heartbeats perfectly happily. The client-side timeout is the only thing that
#: bounds progress; this one bounds the activity around it.
INFERENCE_TIMEOUT = timedelta(minutes=6)
FETCH_TIMEOUT = timedelta(minutes=2)
WRITE_TIMEOUT = timedelta(minutes=2)

#: Without this, a dead worker is invisible until the full timeout above
#: elapses - up to ten minutes for one filter or embed batch. The activity
#: heartbeats every 5s (see `activities._with_heartbeat`); missing four in a
#: row is what tells Temporal the worker is actually gone, versus one slow
#: scheduling tick under load. Detection drops from up to 10 minutes to ~20s.
INFERENCE_HEARTBEAT_TIMEOUT = timedelta(seconds=20)
