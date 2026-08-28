"""The console's organization surface, over HTTP."""

from __future__ import annotations

import uuid

UNKNOWN = "00000000-0000-4000-8000-000000000000"


async def create(client, name: str, parent_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
    response = await client.post(
        "/api/v1/org/nodes",
        json={"name": name, "parent_id": parent_id},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def seeded_user_id(client) -> str:  # type: ignore[no-untyped-def]
    return (await client.get("/api/v1/users")).json()[0]["id"]


async def test_the_tree_comes_back_flat_with_parent_ids(client) -> None:  # type: ignore[no-untyped-def]
    root = await create(client, "Acme")
    await create(client, "Engineering", root)

    response = await client.get("/api/v1/org/nodes")

    assert response.status_code == 200
    nodes = {n["name"]: n for n in response.json()}
    assert nodes["Acme"]["parent_id"] is None
    assert nodes["Engineering"]["parent_id"] == root


async def test_a_department_needs_a_name(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.post("/api/v1/org/nodes", json={"name": ""})).status_code == 422


async def test_creating_under_an_unknown_parent_is_a_404(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.post("/api/v1/org/nodes", json={"name": "Orphan", "parent_id": UNKNOWN})

    assert response.status_code == 404


async def test_renaming_leaves_the_parent_alone(client) -> None:  # type: ignore[no-untyped-def]
    root = await create(client, "Acme")
    child = await create(client, "Engineering", root)

    response = await client.patch(f"/api/v1/org/nodes/{child}", json={"name": "Engineering Group"})

    assert response.status_code == 200
    assert response.json()["name"] == "Engineering Group"
    assert response.json()["parent_id"] == root


async def test_a_parent_without_reparent_is_refused_rather_than_ignored(client) -> None:  # type: ignore[no-untyped-def]
    """A caller who meant to move a department should not get a silent no-op."""
    root = await create(client, "Acme")
    other = await create(client, "Finance")

    response = await client.patch(f"/api/v1/org/nodes/{other}", json={"parent_id": root})

    assert response.status_code == 422


async def test_a_department_cannot_be_moved_under_its_own_child(client) -> None:  # type: ignore[no-untyped-def]
    root = await create(client, "Engineering")
    child = await create(client, "Platform", root)

    response = await client.patch(
        f"/api/v1/org/nodes/{root}", json={"parent_id": child, "reparent": True}
    )

    assert response.status_code == 422


async def test_a_department_can_be_promoted_to_a_root(client) -> None:  # type: ignore[no-untyped-def]
    root = await create(client, "Engineering")
    child = await create(client, "Platform", root)

    response = await client.patch(
        f"/api/v1/org/nodes/{child}", json={"parent_id": None, "reparent": True}
    )

    assert response.status_code == 200
    assert response.json()["parent_id"] is None


async def test_deleting_a_department_promotes_its_children_and_says_how_many(client) -> None:  # type: ignore[no-untyped-def]
    acme = await create(client, "Acme")
    engineering = await create(client, "Engineering", acme)
    platform = await create(client, "Platform", engineering)
    await create(client, "Delivery", engineering)

    response = await client.delete(f"/api/v1/org/nodes/{engineering}")

    assert response.status_code == 200
    assert response.json() == {"id": engineering, "promoted": 2}
    nodes = {n["id"]: n for n in (await client.get("/api/v1/org/nodes")).json()}
    assert nodes[platform]["parent_id"] == acme


async def test_filing_a_person_moves_them_out_of_their_previous_department(client) -> None:  # type: ignore[no-untyped-def]
    engineering = await create(client, "Engineering")
    finance = await create(client, "Finance")
    user_id = await seeded_user_id(client)

    await client.post(f"/api/v1/org/nodes/{engineering}/members", json={"user_id": user_id})
    moved = await client.post(f"/api/v1/org/nodes/{finance}/members", json={"user_id": user_id})

    assert moved.status_code == 200
    assert moved.json()["member_ids"] == [user_id]
    nodes = {n["id"]: n for n in (await client.get("/api/v1/org/nodes")).json()}
    assert nodes[engineering]["member_ids"] == []


async def test_the_directory_shows_the_department_a_person_is_filed_into(client) -> None:  # type: ignore[no-untyped-def]
    engineering = await create(client, "Engineering")
    user_id = await seeded_user_id(client)

    await client.post(f"/api/v1/org/nodes/{engineering}/members", json={"user_id": user_id})

    listed = (await client.get("/api/v1/users")).json()[0]
    assert listed["department_id"] == engineering


async def test_filing_an_unknown_person_is_a_404(client) -> None:  # type: ignore[no-untyped-def]
    node = await create(client, "Engineering")

    response = await client.post(
        f"/api/v1/org/nodes/{node}/members", json={"user_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404


async def test_removing_someone_from_the_wrong_department_is_a_404(client) -> None:  # type: ignore[no-untyped-def]
    engineering = await create(client, "Engineering")
    finance = await create(client, "Finance")
    user_id = await seeded_user_id(client)
    await client.post(f"/api/v1/org/nodes/{engineering}/members", json={"user_id": user_id})

    response = await client.delete(f"/api/v1/org/nodes/{finance}/members/{user_id}")

    assert response.status_code == 404


async def test_connectors_list_every_platform(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.get("/api/v1/connectors")

    assert response.status_code == 200
    platforms = {c["platform"] for c in response.json()}
    assert {"slack", "github", "teams", "email", "linear"} <= platforms
    slack = next(c for c in response.json() if c["platform"] == "slack")
    assert slack["account_count"] == 1
    assert slack["messages_contributed"] == 2
