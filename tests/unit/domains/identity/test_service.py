"""Identity resolution - the mechanism the whole platform claim rests on."""

from __future__ import annotations

import pytest

from app.core.errors import AuthorizationError, NotFoundError
from app.core.pagination import PageParams
from app.core.security.principal import Scope
from app.domains.identity.dto import IdentityCandidate
from app.domains.identity.models import Platform
from app.domains.identity.service import UNKNOWN_EMAIL_DOMAIN, IdentityService
from tests.conftest import make_principal
from tests.factories import make_relation, make_user
from tests.fakes import FakeUnitOfWork


def service(uow, *scopes):  # type: ignore[no-untyped-def]
    return IdentityService(uow, make_principal(*(scopes or (Scope.INGEST_RUN,))))


def candidate(**overrides):  # type: ignore[no-untyped-def]
    defaults = {
        "platform": Platform.SLACK,
        "external_id": "U-ALICE",
        "handle": "alice",
        "email": "alice@example.com",
        "display_name": "Alice Nguyen",
    }
    return IdentityCandidate(**{**defaults, **overrides})


async def test_a_new_identity_provisions_a_user_and_a_relation() -> None:
    uow = FakeUnitOfWork()
    resolution = await service(uow).resolve_or_provision([candidate()])

    assert resolution.users_created == 1
    assert resolution.relations_created == 1
    assert resolution[(Platform.SLACK, "U-ALICE")].user_id == uow.users.users[0].id


async def test_the_same_person_on_two_platforms_collapses_onto_one_user() -> None:
    """This is the product's core claim: one person, one history."""
    uow = FakeUnitOfWork()
    resolution = await service(uow).resolve_or_provision(
        [
            candidate(platform=Platform.SLACK, external_id="U-ALICE"),
            candidate(platform=Platform.GITHUB, external_id="alice-gh"),
        ]
    )

    assert resolution.users_created == 1
    assert resolution.relations_created == 2
    user_ids = {relation.user_id for relation in resolution.relations.values()}
    assert len(user_ids) == 1


async def test_different_people_do_not_merge() -> None:
    uow = FakeUnitOfWork()
    resolution = await service(uow).resolve_or_provision(
        [candidate(), candidate(external_id="U-BEN", email="ben@example.com")]
    )
    assert resolution.users_created == 2


async def test_an_existing_relation_is_reused() -> None:
    user = make_user(email="alice@example.com")
    relation = make_relation(user, platform=Platform.SLACK, external_id="U-ALICE")
    uow = FakeUnitOfWork(users=[user], relations=[relation])

    resolution = await service(uow).resolve_or_provision([candidate()])

    assert resolution.users_created == 0
    assert resolution.relations_created == 0
    assert resolution[(Platform.SLACK, "U-ALICE")] is relation


async def test_duplicate_candidates_are_deduplicated() -> None:
    uow = FakeUnitOfWork()
    resolution = await service(uow).resolve_or_provision([candidate(), candidate()])
    assert resolution.relations_created == 1


async def test_identities_without_an_email_get_a_distinct_synthetic_one() -> None:
    """Two anonymous identities must never merge into one person by accident."""
    uow = FakeUnitOfWork()
    await service(uow).resolve_or_provision(
        [
            candidate(external_id="U-1", email=None),
            candidate(external_id="U-2", email=None),
        ]
    )
    emails = {user.email for user in uow.users.users}
    assert emails == {f"U-1@{UNKNOWN_EMAIL_DOMAIN}", f"U-2@{UNKNOWN_EMAIL_DOMAIN}"}


async def test_provisioning_requires_the_ingest_scope() -> None:
    with pytest.raises(AuthorizationError):
        await service(FakeUnitOfWork(), Scope.INSIGHTS_READ).resolve_or_provision([candidate()])


async def test_listing_users_requires_the_read_scope() -> None:
    with pytest.raises(AuthorizationError):
        await service(FakeUnitOfWork(), Scope.INGEST_RUN).list_users(PageParams())


async def test_getting_a_missing_user_raises_not_found() -> None:
    import uuid

    with pytest.raises(NotFoundError):
        await service(FakeUnitOfWork(), Scope.INSIGHTS_READ).get_user(uuid.uuid4())
