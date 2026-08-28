"""The code that actually runs inside the executor.

Kept in its own module with only module-level functions so it can be pickled by
a `ProcessPoolExecutor` (a bound method or closure could not be). The model is
cached per worker - loading `all-MiniLM-L6-v2` costs ~1s and ~90MB, so it is
loaded once per thread pool / per child process and then reused.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_LOCK = threading.Lock()
_MODELS: dict[str, Any] = {}


def init_worker(model_name: str, torch_threads: int) -> None:
    """Executor `initializer`: pay the model load cost up front, off the loop."""
    load_model(model_name, torch_threads)


def load_model(model_name: str, torch_threads: int) -> Any:
    """Return the process-local model, loading it on first use."""
    with _MODEL_LOCK:
        model = _MODELS.get(model_name)
        if model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            # Bound intra-op parallelism: without this torch grabs every core it
            # can see, which starves the event loop's own thread on small boxes.
            torch.set_num_threads(max(1, torch_threads))
            logger.info("Loading embedding model %s (torch threads=%s)", model_name, torch_threads)
            model = SentenceTransformer(model_name, device="cpu")
            model.eval()
            _MODELS[model_name] = model
        return model


def encode_texts(
    texts: list[str],
    model_name: str,
    torch_threads: int,
    batch_size: int,
) -> list[list[float]]:
    """CPU-bound entry point. Returns L2-normalised vectors as plain lists.

    Vectors are normalised so cosine distance reduces to an inner product, which
    is what the pgvector HNSW index on `messages.embedding` is built for. Plain
    lists (not numpy arrays) are returned so the result is cheap to pickle back
    from a child process.
    """
    import torch

    model = load_model(model_name, torch_threads)
    with torch.inference_mode():
        vectors = model.encode(
            texts,
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    return [vector.tolist() for vector in vectors]


def embedding_dimension(model_name: str, torch_threads: int) -> int:
    return int(load_model(model_name, torch_threads).get_sentence_embedding_dimension())
