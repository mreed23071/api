"""One error envelope, everywhere. The SDK narrows against exactly one type."""

from __future__ import annotations

from tests.api.conftest import READER_HEADERS


def assert_envelope(payload: dict) -> dict:
    assert set(payload) == {"error"}
    error = payload["error"]
    assert {"code", "message", "details", "request_id"} <= set(error)
    return error


async def test_unknown_path_uses_the_envelope(client) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert assert_envelope(response.json())["code"] == "not_found"


async def test_validation_failure_uses_the_envelope(client) -> None:
    response = await client.get(
        "/api/v1/insights/users", params={"limit": 5000}, headers=READER_HEADERS
    )
    assert response.status_code == 422
    error = assert_envelope(response.json())
    assert error["code"] == "validation_failed"
    assert error["details"]["errors"]


async def test_missing_credentials_use_the_envelope(client) -> None:
    response = await client.post("/api/v1/ingestion/runs/slack", json={})
    assert response.status_code == 401
    assert assert_envelope(response.json())["code"] == "unauthenticated"


async def test_insufficient_scope_uses_the_envelope(client) -> None:
    response = await client.get("/api/v1/insights/users", headers={"X-API-Key": "test-ingest-key"})
    assert response.status_code == 403
    error = assert_envelope(response.json())
    assert error["code"] == "forbidden"
    assert "insights:read" in error["details"]["missing_scopes"]


async def test_401_carries_a_valid_challenge_header(client) -> None:
    response = await client.post("/api/v1/ingestion/runs/slack", json={})
    assert "ApiKey" in response.headers["WWW-Authenticate"]


async def test_error_response_carries_the_request_id_for_correlation(client) -> None:
    response = await client.post(
        "/api/v1/ingestion/runs/slack", json={}, headers={"X-Request-Id": "trace-xyz"}
    )
    assert response.json()["error"]["request_id"] == "trace-xyz"


async def test_a_bad_api_key_is_rejected_rather_than_treated_as_anonymous(client) -> None:
    response = await client.get("/api/v1/insights/users", headers={"X-API-Key": "not-a-key"})
    assert response.status_code == 401
