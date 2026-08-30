"""Worker entrypoint: `python -m app.workflows.runner`.

Runs in its own container, separate from the API. That separation is the point
- a slow model call now saturates the worker, not the process serving HTTP.

Concurrency is deliberately tight. Ollama is configured single-threaded, so
letting the worker run many activities at once would only queue them inside
Ollama, where they are invisible and untimeoutable. Keeping the limit here
means the queue is in Temporal, where it can be seen and reasoned about.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import timedelta

from temporalio.worker import Worker

from app.core.config import get_settings
from app.core.db.engine import dispose_engine
from app.core.logging import configure_logging
from app.shared.llm.factory import close_llm_client
from app.workflows import client
from app.workflows.activities import ALL_ACTIVITIES, get_deps
from app.workflows.config import INGESTION_TASK_QUEUE
from app.workflows.ingestion import IngestionWorkflow

logger = logging.getLogger(__name__)


async def main() -> None:
    """Run the worker until SIGTERM, then drain rather than die.

    The API's lifespan has always torn its resources down on the way out; this
    process simply awaited `worker.run()` and was killed. On `docker compose
    stop` that meant SIGTERM went unhandled: in-flight activities were never
    cancelled, so their database connections and httpx transports were never
    released, and the loop printed "Task was destroyed but it is pending!" on
    the way down. Ten seconds later Docker sent SIGKILL.

    The sequence below is the API's, adapted: signal, drain, teardown - all of
    it inside a `finally`, so a crash of the worker itself also releases what it
    was holding.
    """
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    # Build the embedding client and LLM up front, so the first activity does
    # not also pay for model load or pool construction.
    deps = get_deps()
    if settings.embedding_warmup_on_startup:
        await deps.embeddings.warmup()

    temporal = await client.connect(settings)

    worker = Worker(
        temporal,
        task_queue=INGESTION_TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=ALL_ACTIVITIES,
        # One inference at a time - see the module docstring.
        max_concurrent_activities=settings.temporal_max_concurrent_activities,
        # How long Temporal waits for cancelled activities to unwind before it
        # stops caring. Sits inside compose's 45s `stop_grace_period`, so the
        # drain finishes on its own terms rather than being cut short by
        # SIGKILL.
        graceful_shutdown_timeout=timedelta(seconds=30),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Unavailable on Windows outside a container, where this is only ever
        # run by a developer who will use Ctrl-C anyway. In-container it always
        # works, which is the case that matters.
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    logger.info(
        "ingestion worker started",
        extra={
            "task_queue": INGESTION_TASK_QUEUE,
            "llm_provider": deps.llm.provider,
            "embedding_model": deps.embeddings.model_name,
            "max_concurrent_activities": settings.temporal_max_concurrent_activities,
        },
    )

    run_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            logger.info("shutdown signal received; draining worker")
            # Cancels in-flight activities and waits up to
            # `graceful_shutdown_timeout`. The cancellation propagates into
            # `_with_heartbeat`, which now awaits the cancelled call rather than
            # abandoning it - so httpx returns its connection to the transport
            # pool instead of the task being destroyed while pending.
            await worker.shutdown()
        # Surfaces an error from the worker itself; returns cleanly after a
        # successful `shutdown()`.
        await run_task
    finally:
        stop_task.cancel()
        # Each teardown is independent: one failing must not skip the others,
        # and none of them should replace whatever brought us here.
        with suppress(Exception):
            # Synchronous on the port - `EmbeddingClient.shutdown` is the
            # documented counterpart to the `start()` in `get_deps`.
            deps.embeddings.shutdown()
        with suppress(Exception):
            await close_llm_client()
        with suppress(Exception):
            await dispose_engine()
        with suppress(Exception):
            await client.close()
        logger.info("worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
