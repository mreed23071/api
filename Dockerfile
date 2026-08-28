# syntax=docker/dockerfile:1.9
# ---------------------------------------------------------------------------
# Stage 1 - build the virtualenv with uv and bake the embedding model in.
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first: this layer is only invalidated when pyproject.toml or
# uv.lock change. --frozen means the build resolves nothing: it installs exactly
# what the committed lockfile pins, so the image is reproducible.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Bake the sentence-transformer weights into the image: the runtime container
# then needs no network at all, and the first ingestion call is not penalised
# by a cold model download.
ARG EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
ENV HF_HOME=/opt/huggingface \
    EMBEDDING_MODEL_NAME=${EMBEDDING_MODEL_NAME}
RUN /app/.venv/bin/python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBEDDING_MODEL_NAME'])"

# Project source last - it changes on every commit.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2 - slim runtime: no uv, no build tooling, non-root.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /opt/huggingface /opt/huggingface
COPY --chown=app:app src ./src
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app alembic.ini pyproject.toml README.md ./

RUN chmod +x /app/scripts/entrypoint.sh

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
