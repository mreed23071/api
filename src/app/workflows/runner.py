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

from temporalio.worker import Worker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workflows import client
from app.workflows.activities import ALL_ACTIVITIES, get_deps
from app.workflows.config import INGESTION_TASK_QUEUE
from app.workflows.ingestion import IngestionWorkflow

logger = logging.getLogger(__name__)


async def main() -> None:
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
    )
    logger.info(
        "ingestion worker started",
        extra={
            "task_queue": INGESTION_TASK_QUEUE,
            "llm_provider": deps.llm.provider,
            "embedding_model": deps.embeddings.model_name,
            "max_concurrent_activities": settings.temporal_max_concurrent_activities,
        },
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
