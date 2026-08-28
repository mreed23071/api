"""Identity persistence against a real database."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.pagination import PageParams
from app.domains.identity.models import Platform
from tests.factories import make_relation, make_user

pytestmark = pytest.mark.integration


async def test_users_round_trip(uow) -> None:
    user = await uow.users.add(make_user(email="alice@example.com"))
    fetched = await uow.users.get(user.id)
    assert fetched is not None and fetched.email == "alice@example.com"


async def test_email_is_unique(uow) -> None:
    """The merge key. If duplicates were possible, identity resolution breaks."""
    await uow.users.add(make_user(email="dup@example.com"))
    with pytest.raises(IntegrityError):
        await uow.users.add(make_user(email="dup@example.com"))


async def test_an_external_identity_belongs_to_exactly_one_user(uow) -> None:
    first, second = await uow.users.add(make_user()), await uow.users.add(make_user())
    await uow.relations.add(
        make_relation(first, platform=Platform.SLACK, external_id="U-SHARED")
    )
    with pytest.raises(IntegrityError):
        await uow.relations.add(
            make_relation(second, platform=Platform.SLACK, external_id="U-SHARED")
        )


async def test_the_same_external_id_on_a_different_platform_is_fine(uow) -> None:
    user = await uow.users.add(make_user())
    await uow.relations.add(make_relation(user, platform=Platform.SLACK, external_id="shared"))
    await uow.relations.add(make_relation(user, platform=Platform.GITHUB, external_id="shared"))
    assert len(await uow.relations.list_for_user(user.id)) == 2


async def test_resolve_many_is_one_query_for_a_whole_batch(uow) -> None:
    user = await uow.users.add(make_user())
    slack = await uow.relations.add(
        make_relation(user, platform=Platform.SLACK, external_id="U-A")
    )
    github = await uow.relations.add(
        make_relation(user, platform=Platform.GITHUB, external_id="gh-a")
    )

    resolved = await uow.relations.resolve_many(
        [
            (Platform.SLACK, "U-A"),
            (Platform.GITHUB, "gh-a"),
            (Platform.TEAMS, "never-seen"),
        ]
    )
    assert resolved == {
        (Platform.SLACK, "U-A"): slack,
        (Platform.GITHUB, "gh-a"): github,
    }


async def test_resolve_many_with_no_identities_makes_no_query(uow) -> None:
    assert await uow.relations.resolve_many([]) == {}


async def test_listing_excludes_inactive_users_by_default(uow) -> None:
    await uow.users.add(make_user())
    await uow.users.add(make_user(is_active=False))

    active = await uow.users.list_users(PageParams())
    everyone = await uow.users.list_users(PageParams(), active_only=False)

    assert active.total == 1
    assert everyone.total == 2


async def test_pagination_walks_the_whole_set_without_gaps(uow) -> None:
    for _ in range(5):
        await uow.users.add(make_user())

    first = await uow.users.list_users(PageParams(limit=2, offset=0))
    second = await uow.users.list_users(PageParams(limit=2, offset=2))
    third = await uow.users.list_users(PageParams(limit=2, offset=4))

    ids = [u.id for page in (first, second, third) for u in page.items]
    assert len(set(ids)) == 5
    assert first.has_more and second.has_more and not third.has_more


async def test_relations_load_eagerly_when_requested(uow) -> None:
    """`lazy="raise"` means the caller must ask; verify the option works."""
    user = await uow.users.add(make_user())
    await uow.relations.add(make_relation(user))
    uow.session.expire_all()

    page = await uow.users.list_users(PageParams(), with_relations=True)
    assert len(page.items[0].relations) == 1


async def test_relations_raise_rather_than_lazy_load(uow) -> None:
    """Better a loud error in development than a MissingGreenlet in production."""
    user = await uow.users.add(make_user())
    await uow.relations.add(make_relation(user))
    uow.session.expire_all()

    page = await uow.users.list_users(PageParams(), with_relations=False)
    with pytest.raises(Exception):
        _ = page.items[0].relations
