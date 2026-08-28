"""In-process embedding adapter: a sentence-transformers model on the CPU.

No longer the default - see `app.shared.embeddings.factory`. It is kept as an
adapter rather than deleted because it is the only embedder that needs no
network, which makes it the offline fallback and the one CI could use without
standing up Ollama. It is also the reason `torch` is still a dependency.

Non-blocking access to the local embedding model.

`model.encode()` is a synchronous, CPU-bound torch call. Running it directly in
a coroutine would pin the single ASGI event loop thread for the whole batch and
stall every other in-flight request, so every call is dispatched to a dedicated
executor and awaited via `loop.run_in_executor`.

Two executor flavours are available (EMBEDDING_EXECUTOR):

* `thread`  - default. torch releases the GIL inside its kernels, so a small
  thread pool gives real parallelism with one shared copy of the model and no
  IPC. Best choice for the common case.
* `process` - full isolation from the GIL at the cost of one model copy and one
  pickle round-trip per call. Worth it when embedding competes with other
  CPU-heavy Python work in the same container.

Note that FastAPI's own `run_in_threadpool` is deliberately *not* used: that
pool is shared with every sync dependency and `def` endpoint in the app, and
saturating it with minute-long embedding batches would block them too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor

from app.core.config import Settings
from app.shared.embeddings import worker
from app.shared.embeddings.base import EmbeddingError

logger = logging.getLogger(__name__)


class LocalEmbeddingClient:
    """Owns the executor and the lifecycle of the local model."""

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.embedding_model_name
        self._dim = settings.embedding_dim
        self._batch_size = settings.embedding_batch_size
        self._kind = settings.embedding_executor
        self._workers = max(1, settings.embedding_workers)
        self._torch_threads = settings.embedding_torch_threads
        self._executor: Executor | None = None
        self._warm = False

    @property
    def is_ready(self) -> bool:
        """Pool started and the model loaded at least once.

        Used by the readiness probe, which must not pay for a forward pass on
        every check.
        """
        return self._executor is not None and self._warm

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def start(self) -> None:
        if self._executor is not None:
            return
        init_args = (self._model_name, self._torch_threads)
        if self._kind == "process":
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                initializer=worker.init_worker,
                initargs=init_args,
            )
        else:
            self._executor = ThreadPoolExecutor(
                max_workers=self._workers,
                thread_name_prefix="embed",
                initializer=worker.init_worker,
                initargs=init_args,
            )
        logger.info(
            "Embedding executor started (kind=%s workers=%s model=%s)",
            self._kind,
            self._workers,
            self._model_name,
        )

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        self._warm = False

    async def warmup(self) -> None:
        """Load the model during startup so no request pays for it."""
        await self.embed(["warmup"])
        self._warm = True
        logger.info("Embedding model warm.")

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed `texts`, off the event loop. Order is preserved."""
        if not texts:
            return []
        if self._executor is None:
            raise EmbeddingError("LocalEmbeddingClient.start() was never called.")

        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(
            self._executor,
            worker.encode_texts,
            list(texts),
            self._model_name,
            self._torch_threads,
            self._batch_size,
        )

        self._warm = True

        if vectors and len(vectors[0]) != self._dim:
            raise EmbeddingError(
                f"Model {self._model_name} produced {len(vectors[0])}-d vectors but the "
                f"messages.embedding column is {self._dim}-d. Update EMBEDDING_DIM and "
                "add a migration before changing models."
            )
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]
