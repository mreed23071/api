"""Structured logging.

Logs are JSON so they are queryable in whatever aggregator this ends up in, and
every record carries the ambient request id (and ingestion run id, when one is
in scope) without any call site having to pass it. Correlation is the difference
between "an error happened" and "this request failed at this step".
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

#: Set by the request-context middleware; read by the formatter.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
#: Set by the ingestion service for the duration of a run.
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

#: LogRecord attributes that are not user-supplied `extra` fields.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with correlation ids folded in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        run_id = run_id_var.get()
        if run_id:
            payload["run_id"] = run_id

        # Anything passed as logger.info(..., extra={...}) rides along.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Readable single-line output for local development."""

    def format(self, record: logging.LogRecord) -> str:
        request_id = request_id_var.get()
        prefix = f"[{request_id[:8]}] " if request_id else ""
        base = super().format(record)
        return f"{prefix}{base}"


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the root handler. Idempotent - safe to call from tests."""
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            HumanFormatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; make them defer to ours so every line
    # in the container has the same shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
