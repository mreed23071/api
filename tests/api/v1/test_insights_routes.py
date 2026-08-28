"""v1 insights route: auth, pagination envelope, and what is exposed."""

from __future__ import annotations

from tests.api.conftest import INGEST_HEADERS, READER_HEADERS

USERS = "/api/v1/insights/users"


async def test_returns_a_page_of_summaries(client) -> None:
    response = await client.get(USERS, headers=READER_HEADERS)
    assert response.status_code == 200

    body = response.json()
    assert set(body) == {"page", "llm_provider", "llm_model"}
    assert set(body["page"]) == {"items", "total", "limit", "offset", "has_more"}

    entry = body["page"]["items"][0]
    assert entry["user"]["email"] == "alice@example.com"
    assert entry["message_count"] == 2
    assert entry["summary"]
    assert entry["summary_error"] is None
    assert len(entry["relations"]) == 1


async def test_embeddings_are_never_serialised(client) -> None:
    """384 floats per message would be hundreds of KB per page, and unusable."""
    body = (await client.get(USERS, headers=READER_HEADERS)).json()
    assert "embedding" not in str(body)


async def test_pagination_parameters_are_reflected(client) -> None:
    body = (
        await client.get(USERS, params={"limit": 1, "offset": 0}, headers=READER_HEADERS)
    ).json()
    assert body["page"]["limit"] == 1 and body["page"]["offset"] == 0


async def test_pagination_is_bounded(client) -> None:
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        response = await client.get(USERS, params=params, headers=READER_HEADERS)
        assert response.status_code == 422


async def test_messages_per_user_is_bounded(client) -> None:
    assert (
        await client.get(USERS, params={"messages_per_user": 500}, headers=READER_HEADERS)
    ).status_code == 422


async def test_reading_summaries_is_never_anonymous(client) -> None:
    """This endpoint returns names, emails, message text and behavioural profiles."""
    assert (await client.get(USERS)).status_code == 401


async def test_the_scheduler_credential_cannot_read_summaries(client) -> None:
    """Ingestion and reading personal data are separate capabilities."""
    assert (await client.get(USERS, headers=INGEST_HEADERS)).status_code == 403


async def test_a_dev_user_can_read_summaries(client) -> None:
    response = await client.get(USERS, headers={"X-Dev-User": "dev-user"})
    assert response.status_code == 200
