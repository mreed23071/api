"""Pagination primitives shared by every context.

`PageParams` is an input contract; `Paginated` is what a service returns. The
wire representation lives in the API version that publishes it
(`app.api.v1.schemas.common.Page`) so a future version can change the envelope -
to cursors, say - without touching a single service.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class PageParams:
    """Offset pagination.

    Offset degrades on large tables; when that becomes the bottleneck, add a
    `CursorParams` beside this and let services accept either. Callers already
    pass an object rather than two integers, so the change stays local.
    """

    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {self.limit}")
        if self.offset < 0:
            raise ValueError(f"offset must be >= 0, got {self.offset}")


# NB: no `slots=True` here - combining it with a generic is a known CPython
# sharp edge. `PageParams` above is not generic, so it keeps slots.
@dataclass(frozen=True)
class Paginated[T]:
    """A slice of a result set plus the total it was drawn from."""

    items: Sequence[T]
    total: int
    params: PageParams

    @property
    def has_more(self) -> bool:
        return self.params.offset + len(self.items) < self.total

    def map[R](self, fn: Callable[[T], R]) -> Paginated[R]:
        """Project the items, keeping the pagination metadata intact."""
        return Paginated(
            items=[fn(item) for item in self.items], total=self.total, params=self.params
        )
