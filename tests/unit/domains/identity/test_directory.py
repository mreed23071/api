"""Directory behaviour: people, their accounts, and the notes kept about them."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.pagination import PageParams
from app.core.security.principal import Principal
from app.domains.identity.directory import DirectoryService
from app.domains.identity.dto import NewAccount, NewPerson, PersonPatch
from app.domains.identity.models import Platform
from app.domains.organization.models import OrgNode, OrgNodeMember
from tests.factories import make_message, make_relation, make_user
from tests.fakes.uow import FakeUnitOfWork


def service(**kwargs) -> tuple[DirectoryService, FakeUnitOfWork]:  # type: ignore[no-untyped-def]
    uow = FakeUnitOfWork(**kwargs)
    return DirectoryService(uow, Principal.anonymous()), uow


# -- people ---------------------------------------------------------------


async def test_the_directory_folds_in_platforms_counts_and_department() -> None:
    amara = make_user(full_name="Amara Okafor")
    slack = make_relation(amara, platform=Platform.SLACK)
    github = make_relation(amara, platform=Platform.GITHUB)
    node = OrgNode(name="Platform")
    node.id, node.created_at = uuid.uuid4(), datetime.now(UTC)

    svc, _ = service(
        users=[amara],
        relations=[slack, github],
        messages=[make_message(amara), make_message(amara)],
        org_nodes=[node],
        org_members=[OrgNodeMember(id=uuid.uuid4(), org_node_id=node.id, user_id=amara.id)],
    )

    view = (await svc.list_people())[0]

    assert set(view.platforms) == {Platform.SLACK, Platform.GITHUB}
    assert view.message_count == 2
    assert view.department_id == node.id


async def test_an_unlinked_account_contributes_no_platform_to_anyone() -> None:
    amara = make_user()
    orphan = make_relation(amara, user_id=None, platform=Platform.TEAMS)
    svc, _ = service(users=[amara], relations=[orphan])

    assert (await svc.list_people())[0].platforms == []


async def test_the_paged_directory_folds_in_the_same_aggregates_as_the_unpaged_one() -> None:
    """`list_people_page` scopes its four queries to the page - the result
    should still be indistinguishable from `list_people`'s."""
    amara = make_user(full_name="Amara Okafor")
    slack = make_relation(amara, platform=Platform.SLACK)
    github = make_relation(amara, platform=Platform.GITHUB)
    node = OrgNode(name="Platform")
    node.id, node.created_at = uuid.uuid4(), datetime.now(UTC)

    svc, _ = service(
        users=[amara],
        relations=[slack, github],
        messages=[make_message(amara), make_message(amara)],
        org_nodes=[node],
        org_members=[OrgNodeMember(id=uuid.uuid4(), org_node_id=node.id, user_id=amara.id)],
    )

    page = await svc.list_people_page(PageParams(limit=20))

    assert page.total == 1
    view = page.items[0]
    assert set(view.platforms) == {Platform.SLACK, Platform.GITHUB}
    assert view.message_count == 2
    assert view.department_id == node.id


async def test_the_paged_directory_leaves_other_pages_out_of_the_aggregates() -> None:
    """A person's message count must not leak in from someone on another page."""
    amara = make_user(full_name="Amara Okafor")
    bilal = make_user(full_name="Bilal Rahman")
    svc, _ = service(
        users=[amara, bilal],
        messages=[make_message(amara), make_message(amara), make_message(bilal)],
    )

    first_page = await svc.list_people_page(PageParams(limit=1, offset=0))
    second_page = await svc.list_people_page(PageParams(limit=1, offset=1))

    assert first_page.total == 2
    assert first_page.items[0].message_count == 2
    assert second_page.items[0].message_count == 1


async def test_the_paged_directory_offset_moves_the_window() -> None:
    svc, _ = service(users=[make_user(full_name=f"Person {i}") for i in range(3)])

    page = await svc.list_people_page(PageParams(limit=1, offset=2))

    assert page.total == 3
    assert len(page.items) == 1
    assert not page.has_more


async def test_creating_a_person_fills_in_the_defaults_a_form_leaves_blank() -> None:
    svc, _ = service()

    person = await svc.create_person(
        NewPerson(full_name="Amara Okafor", email="Amara@Example.com")
    )

    assert person.email == "amara@example.com", "email is the merge key; normalise it"
    assert person.display_name == "Amara", "what a colleague would call her"
    assert person.timezone == "UTC"
    assert person.employment_start is not None


async def test_two_people_cannot_share_an_email_address() -> None:
    existing = make_user(email="amara@example.com")
    svc, _ = service(users=[existing])

    with pytest.raises(ConflictError):
        await svc.create_person(NewPerson(full_name="Someone Else", email="AMARA@example.com"))


async def test_a_person_needs_a_name_and_an_email() -> None:
    svc, _ = service()

    with pytest.raises(ValidationError):
        await svc.create_person(NewPerson(full_name="  ", email="a@example.com"))


async def test_a_patch_touches_only_the_fields_it_names() -> None:
    amara = make_user(full_name="Amara Okafor", job_title="Engineer")
    svc, _ = service(users=[amara])

    updated = await svc.update_person(amara.id, PersonPatch(job_title="Staff Engineer"))

    assert updated.job_title == "Staff Engineer"
    assert updated.full_name == "Amara Okafor"


async def test_erasure_removes_everything_and_reports_what_it_removed() -> None:
    """The returned counts are the only record that survives the cascade."""
    amara = make_user()
    slack = make_relation(amara)
    svc, uow = service(
        users=[amara],
        relations=[slack],
        messages=[make_message(amara), make_message(amara), make_message(amara)],
    )

    result = await svc.forget_person(amara.id)

    assert result == type(result)(deleted_messages=3, deleted_accounts=1)
    assert uow.users.users == []
    assert uow.messages.messages == []
    assert uow.relations.relations == []


async def test_forgetting_someone_who_does_not_exist_is_a_not_found() -> None:
    svc, _ = service()

    with pytest.raises(NotFoundError):
        await svc.forget_person(uuid.uuid4())


# -- accounts -------------------------------------------------------------


async def test_linking_an_account_reattributes_every_message_it_ever_sent() -> None:
    """The operation the whole two-foreign-key design exists to make safe."""
    amara = make_user()
    orphan = make_relation(amara, user_id=None)
    stranded = make_message(amara, sender_user_id=None, sender_relation_id=orphan.id)
    svc, uow = service(users=[amara], relations=[orphan], messages=[stranded])

    await svc.link_account(orphan.id, amara.id)

    assert stranded.sender_user_id == amara.id
    assert stranded.sender_relation_id == orphan.id, "provenance never moves"
    assert uow.relations.relations[0].user_id == amara.id


async def test_the_first_account_linked_to_someone_becomes_their_primary() -> None:
    amara = make_user()
    first = make_relation(amara, user_id=None, is_primary=False)
    svc, _ = service(users=[amara], relations=[first])

    linked = await svc.link_account(first.id, amara.id)

    assert linked.is_primary


async def test_a_later_account_does_not_displace_an_existing_primary() -> None:
    amara = make_user()
    primary = make_relation(amara, is_primary=True)
    second = make_relation(amara, user_id=None, platform=Platform.GITHUB)
    svc, _ = service(users=[amara], relations=[primary, second])

    linked = await svc.link_account(second.id, amara.id)

    assert not linked.is_primary
    assert primary.is_primary


async def test_unlinking_returns_the_messages_to_the_unresolved_pool() -> None:
    amara = make_user()
    slack = make_relation(amara, is_primary=True)
    message = make_message(amara, sender_relation_id=slack.id)
    svc, _ = service(users=[amara], relations=[slack], messages=[message])

    unlinked = await svc.unlink_account(slack.id)

    assert unlinked.user_id is None
    assert message.sender_user_id is None
    assert not unlinked.is_primary, "a detached account is nobody's primary"


async def test_deleting_an_account_takes_its_messages_with_it() -> None:
    """Unlike unlinking, which keeps them and forgets who sent them."""
    amara = make_user()
    slack = make_relation(amara)
    svc, uow = service(
        users=[amara],
        relations=[slack],
        messages=[make_message(amara, sender_relation_id=slack.id), make_message(amara)],
    )

    result = await svc.delete_account(slack.id)

    assert result.deleted_messages == 1
    assert len(uow.messages.messages) == 1
    assert uow.relations.relations == []


async def test_an_unlinked_account_is_listed_with_the_context_needed_to_claim_it() -> None:
    amara = make_user()
    orphan = make_relation(amara, user_id=None)
    recent = datetime.now(UTC)
    svc, _ = service(
        users=[amara],
        relations=[orphan],
        messages=[
            make_message(amara, sender_relation_id=orphan.id, sent_at=recent - timedelta(days=2)),
            make_message(amara, sender_relation_id=orphan.id, sent_at=recent),
        ],
    )

    view = (await svc.list_unlinked_accounts())[0]

    assert view.message_count == 2
    assert view.last_seen_at == recent


async def test_creating_an_account_by_hand_synthesises_a_unique_external_id() -> None:
    """Nobody has authenticated against it, so the platform has given us nothing."""
    amara = make_user()
    svc, _ = service(users=[amara])

    account = await svc.create_account(
        NewAccount(user_id=amara.id, platform=Platform.LINEAR, external_handle="amara")
    )

    assert account.external_id.startswith("LINEAR-")
    assert account.is_primary


async def test_linking_to_an_unknown_person_is_refused() -> None:
    amara = make_user()
    orphan = make_relation(amara, user_id=None)
    svc, _ = service(users=[amara], relations=[orphan])

    with pytest.raises(NotFoundError):
        await svc.link_account(orphan.id, uuid.uuid4())


# -- notes ----------------------------------------------------------------


async def test_notes_come_back_newest_first() -> None:
    amara = make_user()
    svc, _ = service(users=[amara])
    await svc.add_note(amara.id, "First observation.", "michael")
    await svc.add_note(amara.id, "Second observation.", "michael")

    notes = await svc.list_notes(amara.id)

    assert [n.body for n in notes][0] == "Second observation."


async def test_an_empty_note_is_refused() -> None:
    amara = make_user()
    svc, _ = service(users=[amara])

    with pytest.raises(ValidationError):
        await svc.add_note(amara.id, "   ", "michael")


async def test_deleting_a_note_that_does_not_exist_is_a_not_found() -> None:
    svc, _ = service()

    with pytest.raises(NotFoundError):
        await svc.delete_note(uuid.uuid4())
