"""The console's directory surface, over HTTP.

These run against the fake unit of work the `app` fixture installs, so they
exercise routing, schema validation and status codes without a database. The SQL
those services emit is covered separately by the integration suite.
"""

from __future__ import annotations

import uuid

UNKNOWN = "00000000-0000-4000-8000-000000000000"


async def seeded_user_id(client) -> str:  # type: ignore[no-untyped-def]
    response = await client.get("/api/v1/users")
    return response.json()[0]["id"]


# -- people ----------------------------------------------------------------


async def test_the_directory_lists_everyone_with_their_counts(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.get("/api/v1/users")

    assert response.status_code == 200
    person = response.json()[0]
    assert person["full_name"] == "Alice Nguyen"
    assert person["message_count"] == 2
    assert person["platforms"] == ["slack"]
    assert person["department_id"] is None


async def test_the_directory_stays_unpaged_without_a_limit(client) -> None:  # type: ignore[no-untyped-def]
    """No `limit` means every consumer that needs the whole roster - the org
    chart, sender-name lookups, the account-linking picker - keeps working
    exactly as before."""
    response = await client.get("/api/v1/users")

    assert response.status_code == 200
    assert "X-Total-Count" not in response.headers
    assert isinstance(response.json(), list)


async def test_the_directory_pages_when_a_limit_is_given(client) -> None:  # type: ignore[no-untyped-def]
    for i in range(2):
        created = await client.post(
            "/api/v1/users",
            json={"full_name": f"Extra Person {i}", "email": f"extra{i}@example.com"},
        )
        assert created.status_code == 201

    first = await client.get("/api/v1/users", params={"limit": 1, "offset": 0})
    second = await client.get("/api/v1/users", params={"limit": 1, "offset": 1})

    assert len(first.json()) == 1
    assert len(second.json()) == 1
    assert first.json()[0]["id"] != second.json()[0]["id"]
    assert first.headers["X-Total-Count"] == "3"
    assert first.headers["X-Has-More"] == "true"

    last = await client.get("/api/v1/users", params={"limit": 1, "offset": 2})
    assert last.headers["X-Has-More"] == "false"


async def test_fetching_one_person(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)

    response = await client.get(f"/api/v1/users/{user_id}")

    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"


async def test_fetching_someone_who_does_not_exist_is_a_404(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.get(f"/api/v1/users/{UNKNOWN}")).status_code == 404


async def test_a_malformed_id_is_a_422_not_a_404(client) -> None:  # type: ignore[no-untyped-def]
    """Path validation runs before the handler, so this never reaches the service."""
    assert (await client.get("/api/v1/users/not-a-uuid")).status_code == 422


async def test_creating_a_person_normalises_their_email(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.post(
        "/api/v1/users",
        json={"full_name": "Amara Okafor", "email": "Amara@Example.com"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "amara@example.com"
    assert body["display_name"] == "Amara"


async def test_a_duplicate_email_is_a_409(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.post(
        "/api/v1/users",
        json={"full_name": "Someone Else", "email": "ALICE@example.com"},
    )

    assert response.status_code == 409


async def test_an_invalid_email_is_rejected_before_the_service_sees_it(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.post(
        "/api/v1/users", json={"full_name": "Nobody", "email": "not-an-email"}
    )

    assert response.status_code == 422


async def test_a_patch_changes_only_what_it_names(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)

    response = await client.patch(
        f"/api/v1/users/{user_id}", json={"job_title": "Staff Engineer"}
    )

    assert response.status_code == 200
    assert response.json()["job_title"] == "Staff Engineer"
    assert response.json()["full_name"] == "Alice Nguyen"


async def test_erasure_reports_what_it_destroyed(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)

    response = await client.delete(f"/api/v1/users/{user_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted_messages": 2, "deleted_accounts": 1}
    assert (await client.get("/api/v1/users")).json() == []


# -- accounts and messages -------------------------------------------------


async def test_a_persons_accounts_and_messages_are_reachable(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)

    accounts = await client.get(f"/api/v1/users/{user_id}/accounts")
    messages = await client.get(f"/api/v1/users/{user_id}/messages")

    assert len(accounts.json()) == 1
    assert len(messages.json()) == 2
    assert messages.json()[0]["kind"] == "message"


async def test_unlinked_accounts_start_empty_and_appear_after_unlinking(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)
    account_id = (await client.get(f"/api/v1/users/{user_id}/accounts")).json()[0]["id"]

    assert (await client.get("/api/v1/accounts/unlinked")).json() == []

    unlinked = await client.post(f"/api/v1/accounts/{account_id}/unlink")

    assert unlinked.status_code == 200
    assert unlinked.json()["user_id"] is None
    orphans = (await client.get("/api/v1/accounts/unlinked")).json()
    assert len(orphans) == 1
    assert orphans[0]["message_count"] == 2


async def test_linking_reattributes_the_messages(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)
    account_id = (await client.get(f"/api/v1/users/{user_id}/accounts")).json()[0]["id"]
    await client.post(f"/api/v1/accounts/{account_id}/unlink")

    relinked = await client.post(
        f"/api/v1/accounts/{account_id}/link", json={"user_id": user_id}
    )

    assert relinked.status_code == 200
    assert relinked.json()["user_id"] == user_id
    assert len((await client.get(f"/api/v1/users/{user_id}/messages")).json()) == 2


async def test_deleting_an_account_destroys_its_messages(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)
    account_id = (await client.get(f"/api/v1/users/{user_id}/accounts")).json()[0]["id"]

    response = await client.delete(f"/api/v1/accounts/{account_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted_messages": 2}


async def test_browsing_messages_applies_its_filters(client) -> None:  # type: ignore[no-untyped-def]
    everything = await client.get("/api/v1/messages")
    filtered = await client.get("/api/v1/messages", params={"platform": "github"})

    assert everything.json()["total"] == 2
    assert len(everything.json()["items"]) == 2
    assert filtered.json()["items"] == []
    assert filtered.json()["total"] == 0


async def test_browsing_messages_is_paged(client) -> None:  # type: ignore[no-untyped-def]
    first = await client.get("/api/v1/messages", params={"limit": 1, "offset": 0})
    second = await client.get("/api/v1/messages", params={"limit": 1, "offset": 1})

    assert len(first.json()["items"]) == 1
    assert len(second.json()["items"]) == 1
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    assert first.json()["total"] == 2
    assert first.json()["has_more"] is True
    assert second.json()["has_more"] is False


# -- notes -----------------------------------------------------------------


async def test_a_note_can_be_written_read_back_and_deleted(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)

    created = await client.post(
        f"/api/v1/users/{user_id}/notes",
        json={"body": "Leads the billing migration.", "author": "michael"},
    )
    assert created.status_code == 201
    note_id = created.json()["id"]

    listed = await client.get(f"/api/v1/users/{user_id}/notes")
    assert [n["id"] for n in listed.json()] == [note_id]

    deleted = await client.delete(f"/api/v1/notes/{note_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": note_id}
    assert (await client.get(f"/api/v1/users/{user_id}/notes")).json() == []


async def test_an_empty_note_is_rejected(client) -> None:  # type: ignore[no-untyped-def]
    user_id = await seeded_user_id(client)

    response = await client.post(
        f"/api/v1/users/{user_id}/notes", json={"body": "", "author": "michael"}
    )

    assert response.status_code == 422


async def test_deleting_an_unknown_note_is_a_404(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.delete(f"/api/v1/notes/{uuid.uuid4()}")).status_code == 404
