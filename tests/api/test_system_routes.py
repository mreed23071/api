"""Probes are an infrastructure contract; their shape must not drift."""

from __future__ import annotations


async def test_health_is_unauthenticated_and_unversioned(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] and body["environment"]


async def test_readiness_reports_503_when_a_dependency_is_down(client) -> None:
    """A probe that always answers 200 never removes a broken pod from rotation."""
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_readiness_is_ok_once_the_pool_is_up(client, embeddings, monkeypatch) -> None:
    embeddings.start()
    monkeypatch.setattr(
        "app.api.system.get_embedding_service", lambda: embeddings, raising=True
    )

    response = await client.get("/ready")
    # The database is genuinely unavailable in this suite, so readiness stays
    # degraded - but the embedding flag must now be true.
    assert response.json()["embeddings"] is True


async def test_every_response_carries_a_request_id(client) -> None:
    response = await client.get("/health")
    assert response.headers["X-Request-Id"]


async def test_an_inbound_request_id_is_propagated(client) -> None:
    response = await client.get("/health", headers={"X-Request-Id": "trace-abc"})
    assert response.headers["X-Request-Id"] == "trace-abc"


async def test_version_index_advertises_v1(client) -> None:
    response = await client.get("/api/versions")
    assert response.status_code == 200
    body = response.json()
    assert body["default"] == "v1"
    assert {v["name"] for v in body["versions"]} == {"v1"}
    assert body["versions"][0]["status"] == "stable"
