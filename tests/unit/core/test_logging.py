"""Structured logs and request correlation."""

from __future__ import annotations

import json
import logging

from app.core.logging import JsonFormatter, request_id_var, run_id_var


def record(message: str = "hello", **extra) -> logging.LogRecord:  # type: ignore[no-untyped-def]
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_output_is_one_json_object() -> None:
    payload = json.loads(JsonFormatter().format(record()))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["timestamp"]


def test_request_id_is_folded_in_without_the_call_site_passing_it() -> None:
    token = request_id_var.set("req-123")
    try:
        payload = json.loads(JsonFormatter().format(record()))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "req-123"


def test_run_id_is_folded_in_when_a_run_is_in_scope() -> None:
    token = run_id_var.set("run-abc")
    try:
        payload = json.loads(JsonFormatter().format(record()))
    finally:
        run_id_var.reset(token)
    assert payload["run_id"] == "run-abc"


def test_correlation_ids_are_absent_when_not_set() -> None:
    payload = json.loads(JsonFormatter().format(record()))
    assert "request_id" not in payload and "run_id" not in payload


def test_extra_fields_ride_along() -> None:
    payload = json.loads(JsonFormatter().format(record(fetched=12, source="mock")))
    assert payload["fetched"] == 12 and payload["source"] == "mock"


def test_exceptions_are_serialised() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = record("failed")
        rec.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in payload["exception"]
