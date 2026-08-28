"""Shapes shared by every v1 endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.pagination import Paginated

T = TypeVar("T")
S = TypeVar("S", bound=BaseModel)


class Page(BaseModel, Generic[S]):
    """The v1 pagination envelope.

    Offset-based. A future version can switch to cursors by publishing a
    different envelope here; services return `Paginated`, which carries enough
    information for either.
    """

    items: list[S]
    total: int = Field(description="Total matching records, ignoring pagination.")
    limit: int
    offset: int
    has_more: bool = Field(description="True when another page exists after this one.")

    @classmethod
    def build(cls, page: Paginated[T], mapper: Callable[[T], S]) -> "Page[S]":
        return cls(
            items=[mapper(item) for item in page.items],
            total=page.total,
            limit=page.params.limit,
            offset=page.params.offset,
            has_more=page.has_more,
        )
