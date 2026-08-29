"""Tree arithmetic. Pure functions, no database, no fixtures."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domains.organization.tree import (
    children_index,
    depth_by_id,
    descendant_ids,
    eligible_parents,
    reorder,
    would_create_cycle,
)


@dataclass
class Node:
    """Minimal stand-in for the ORM entity: the shape the module actually needs."""

    id: uuid.UUID
    parent_id: uuid.UUID | None = None


ACME, ENGINEERING, CTO, PLATFORM, FINANCE = (uuid.uuid4() for _ in range(5))


def tree() -> list[Node]:
    """acme -> engineering -> cto -> platform, and acme -> finance."""
    return [
        Node(ACME),
        Node(ENGINEERING, ACME),
        Node(CTO, ENGINEERING),
        Node(PLATFORM, CTO),
        Node(FINANCE, ACME),
    ]


def test_roots_are_indexed_under_none() -> None:
    assert children_index(tree())[None] == [ACME]


def test_descendants_reach_all_the_way_down() -> None:
    assert descendant_ids(tree(), ENGINEERING) == {CTO, PLATFORM}


def test_descendants_exclude_the_node_itself() -> None:
    assert ENGINEERING not in descendant_ids(tree(), ENGINEERING)


def test_a_leaf_has_no_descendants() -> None:
    assert descendant_ids(tree(), PLATFORM) == set()


def test_moving_to_a_root_is_always_allowed() -> None:
    assert not would_create_cycle(tree(), ENGINEERING, None)


def test_a_node_cannot_report_to_itself() -> None:
    assert would_create_cycle(tree(), ENGINEERING, ENGINEERING)


def test_a_node_cannot_report_to_its_own_descendant() -> None:
    """The move that would detach a whole subtree from the tree."""
    assert would_create_cycle(tree(), ENGINEERING, PLATFORM)


def test_moving_sideways_is_allowed() -> None:
    assert not would_create_cycle(tree(), FINANCE, PLATFORM)


def test_depth_counts_edges_from_the_root() -> None:
    depths = depth_by_id(tree())
    assert depths[ACME] == 0
    assert depths[ENGINEERING] == 1
    assert depths[PLATFORM] == 3


def test_eligible_parents_exclude_the_node_and_everything_beneath_it() -> None:
    """Offering an impossible parent and rejecting it later is a worse UI."""
    assert set(eligible_parents(tree(), ENGINEERING)) == {ACME, FINANCE}


def test_a_cycle_already_in_the_data_terminates_rather_than_hanging() -> None:
    """Malformed data should give a wrong answer that returns, never a hang."""
    a, b = uuid.uuid4(), uuid.uuid4()
    broken = [Node(a, b), Node(b, a)]

    assert descendant_ids(broken, a) == {a, b}
    assert depth_by_id(broken).keys() == {a, b}


# -- reorder -----------------------------------------------------------------

A, B, C = (uuid.uuid4() for _ in range(3))


def test_moving_forward() -> None:
    assert reorder([A, B, C], A, 1) == [B, A, C]


def test_moving_backward() -> None:
    assert reorder([A, B, C], C, 0) == [C, A, B]


def test_moving_to_the_start() -> None:
    assert reorder([A, B, C], C, 0)[0] == C


def test_moving_to_the_end() -> None:
    assert reorder([A, B, C], A, 2) == [B, C, A]


def test_moving_to_its_own_position_is_a_no_op() -> None:
    assert reorder([A, B, C], B, 1) == [A, B, C]


def test_a_single_child_stays_put() -> None:
    assert reorder([A], A, 0) == [A]


def test_an_out_of_range_target_is_clamped_not_rejected() -> None:
    """A drop computed against a slightly stale list should still land
    somewhere sane rather than raise."""
    assert reorder([A, B, C], A, 99) == [B, C, A]
    assert reorder([A, B, C], A, -5) == [A, B, C]


def test_moving_in_from_a_different_parents_list() -> None:
    """The reparent case: `moved_id` was never in `children` to begin with -
    it is inserted fresh rather than treated as a no-op or an error."""
    assert reorder([A, B], C, 1) == [A, C, B]
