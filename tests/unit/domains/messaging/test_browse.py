"""The message browser's filters.

Each test sets one filter and asserts what survives it, so a failure names the
filter that broke rather than "browsing is wrong".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.pagination import PageParams
from app.core.security.principal import Principal
from app.domains.identity.models import Platform
from app.domains.messaging.dto import MessageFilters
from app.domains.messaging.service import MessageService
from tests.factories import make_message, make_user
from tests.fakes.uow import FakeUnitOfWork

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

#: A page big enough that none of these small corpora ever get truncated by it.
ALL = PageParams(limit=100)


def corpus() -> tuple[MessageService, FakeUnitOfWork]:
    """Four messages that differ in exactly one dimension each."""
    amara = make_user()
    messages = [
        make_message(
            amara,
            platform=Platform.SLACK,
            filter_category="business",
            content="The deploy is green.",
            sent_at=NOW,
        ),
        make_message(
            amara,
            platform=Platform.GITHUB,
            filter_category="business",
            content="Merged the migration branch.",
            sent_at=NOW - timedelta(days=1),
        ),
        make_message(
            amara,
            platform=Platform.SLACK,
            filter_category="personal",
            content="Lunch at one?",
            sent_at=NOW - timedelta(days=5),
        ),
        make_message(
            amara,
            platform=Platform.TEAMS,
            filter_category="unclear",
            content="Discount is 50% off list.",
            sent_at=NOW - timedelta(days=30),
        ),
    ]
    uow = FakeUnitOfWork(users=[amara], messages=messages)
    return MessageService(uow, Principal.anonymous()), uow


async def test_no_filters_returns_everything_newest_first() -> None:
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(), ALL)

    assert page.total == 4
    assert len(page.items) == 4
    assert page.items[0].content == "The deploy is green."


async def test_filtering_by_platform() -> None:
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(platform=Platform.SLACK), ALL)

    assert {m.platform for m in page.items} == {Platform.SLACK}
    assert page.total == 2


async def test_filtering_by_category() -> None:
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(category="personal"), ALL)

    assert [m.content for m in page.items] == ["Lunch at one?"]


async def test_the_date_bounds_are_inclusive() -> None:
    """A message sent exactly on the boundary is in range, not out of it."""
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(sent_from=NOW), ALL)

    assert [m.content for m in page.items] == ["The deploy is green."]


async def test_a_date_window_narrows_from_both_ends() -> None:
    svc, _ = corpus()

    page = await svc.browse(
        MessageFilters(sent_from=NOW - timedelta(days=6), sent_to=NOW - timedelta(days=1)), ALL
    )

    assert page.total == 2


async def test_search_is_case_insensitive() -> None:
    svc, _ = corpus()

    assert (await svc.browse(MessageFilters(search="DEPLOY"), ALL)).total == 1


async def test_search_ignores_surrounding_whitespace() -> None:
    svc, _ = corpus()

    assert (await svc.browse(MessageFilters(search="  merged  "), ALL)).total == 1


async def test_a_percent_sign_in_a_search_is_a_literal_character() -> None:
    """`%` is a SQL wildcard. Unescaped, this search would match everything."""
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(search="50%"), ALL)

    assert [m.content for m in page.items] == ["Discount is 50% off list."]


async def test_filters_combine() -> None:
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(platform=Platform.SLACK, category="business"), ALL)

    assert [m.content for m in page.items] == ["The deploy is green."]


async def test_the_limit_caps_what_comes_back_but_total_still_counts_everything() -> None:
    svc, _ = corpus()

    page = await svc.browse(MessageFilters(), PageParams(limit=2))

    assert len(page.items) == 2
    assert page.total == 4
    assert page.has_more


async def test_offset_moves_the_window() -> None:
    svc, _ = corpus()

    first = await svc.browse(MessageFilters(), PageParams(limit=2, offset=0))
    second = await svc.browse(MessageFilters(), PageParams(limit=2, offset=2))

    assert [m.content for m in first.items] != [m.content for m in second.items]
    assert not second.has_more
