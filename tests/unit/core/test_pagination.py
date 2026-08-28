from __future__ import annotations

import pytest

from app.core.pagination import MAX_LIMIT, PageParams, Paginated


def test_defaults_are_sane() -> None:
    params = PageParams()
    assert params.limit == 20
    assert params.offset == 0


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
def test_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        PageParams(limit=limit)


def test_offset_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="offset must be"):
        PageParams(offset=-1)


def test_has_more_is_true_when_the_window_ends_before_the_total() -> None:
    page = Paginated(items=[1, 2], total=10, params=PageParams(limit=2, offset=0))
    assert page.has_more is True


def test_has_more_is_false_on_the_last_page() -> None:
    page = Paginated(items=[9, 10], total=10, params=PageParams(limit=2, offset=8))
    assert page.has_more is False


def test_map_preserves_pagination_metadata() -> None:
    page = Paginated(items=[1, 2], total=10, params=PageParams(limit=2, offset=4))
    mapped = page.map(str)
    assert mapped.items == ["1", "2"]
    assert (mapped.total, mapped.params.offset) == (10, 4)
