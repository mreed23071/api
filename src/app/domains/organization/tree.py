"""Tree arithmetic over an adjacency list.

Pure functions over anything with an `id` and a `parent_id`, so they can be
tested without a database and reused by whatever needs them - the service, a
migration, or the authorization evaluator when it starts filtering by subtree.

The console computes the same answers client-side to render the hierarchy. Both
implementations exist deliberately: the client needs them to draw, the server
needs them to enforce. What must never differ is the *rule*, which is why the
cycle definition lives in one function here rather than inline at a call site.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol


class HasParent(Protocol):
    """Structural shape this module needs: an id, and where it hangs.

    A `Protocol` is Python's version of a structural interface - "anything with
    these attributes", with nothing to inherit and nothing to register. That is
    what lets these functions run against real database rows in production and
    against three-line stand-ins in the tests, with no adapter between them.
    """

    id: uuid.UUID
    parent_id: uuid.UUID | None


def children_index(nodes: Iterable[HasParent]) -> Mapping[uuid.UUID | None, list[uuid.UUID]]:
    """Group node ids by their parent, so children can be looked up directly.

    Returns a dictionary where each key is a parent id and each value is the
    list of ids directly beneath it. Nodes with no parent are collected under
    the key `None`, which is how the roots are found.

    `setdefault(key, []).append(x)` is the usual Python way to add to a list
    inside a dictionary without checking first whether the key exists.
    """
    index: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for node in nodes:
        index.setdefault(node.parent_id, []).append(node.id)
    return index


def descendant_ids(nodes: Sequence[HasParent], node_id: uuid.UUID) -> set[uuid.UUID]:
    """Return the ids of every node beneath `node_id`, excluding itself.

    Walks down the tree one level at a time using a queue: take a node off,
    record it, and push its children on. When the queue empties, everything
    reachable has been found.

    The `found` set does double duty. It is the answer, and it is the guard
    against revisiting a node - which matters because if the stored data ever
    contained a loop, a walk without that check would never finish. Malformed
    data should give a wrong answer that returns, never a request that hangs.
    """
    index = children_index(nodes)
    found: set[uuid.UUID] = set()
    queue = list(index.get(node_id, ()))
    while queue:
        current = queue.pop()
        if current in found:
            continue
        found.add(current)
        queue.extend(index.get(current, ()))
    return found


def would_create_cycle(
    nodes: Sequence[HasParent],
    node_id: uuid.UUID,
    next_parent_id: uuid.UUID | None,
) -> bool:
    """Would moving `node_id` under `next_parent_id` break the hierarchy?

    A tree stops being a tree in exactly two ways here: a department set to
    report to itself, or set to report to one of its own sub-departments -
    which would cut that whole branch loose from the rest of the organization.

    Moving to `None` means "make this a root", which is always legal.

    This is the rule the API enforces on every re-parent, and the same rule that
    decides which options the console offers in the first place.
    """
    if next_parent_id is None:
        return False
    if next_parent_id == node_id:
        return True
    return next_parent_id in descendant_ids(nodes, node_id)


def depth_by_id(nodes: Sequence[HasParent]) -> dict[uuid.UUID, int]:
    """How many levels below a root each node sits. A root is depth 0.

    Walks upward from each node collecting the chain of ancestors, then fills in
    the depths from the top down. Results are kept in `depths` as it goes, so a
    node already reached by an earlier walk stops the climb immediately - each
    node is visited once no matter how many share ancestors.

    Used for indentation in the tree view.
    """
    parent_of = {node.id: node.parent_id for node in nodes}
    depths: dict[uuid.UUID, int] = {}

    for start in parent_of:
        chain: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        cursor: uuid.UUID | None = start
        while cursor is not None and cursor in parent_of and cursor not in depths:
            if cursor in seen:
                # A cycle. Treat the entry point as a root rather than looping.
                cursor = None
                break
            seen.add(cursor)
            chain.append(cursor)
            cursor = parent_of[cursor]

        base = depths[cursor] + 1 if cursor is not None and cursor in depths else 0
        for offset, node_id in enumerate(reversed(chain)):
            depths[node_id] = base + offset
    return depths


def reorder(
    children: Sequence[uuid.UUID], moved_id: uuid.UUID, target_index: int
) -> list[uuid.UUID]:
    """`children`, with `moved_id` removed and reinserted at `target_index`.

    `target_index` is clamped rather than rejected - a drop computed against a
    slightly stale list (one more sibling than the caller expects, say) should
    still land somewhere sane rather than raise. `moved_id` not already being
    in `children` is fine too: that is exactly the reparent case, where the
    node is moving in from a different parent's list.

    The caller always writes the *entire* returned list back as 0..n-1 - this
    function only decides the order, not the numbering.
    """
    remaining = [child_id for child_id in children if child_id != moved_id]
    index = max(0, min(target_index, len(remaining)))
    remaining.insert(index, moved_id)
    return remaining


def eligible_parents(nodes: Sequence[HasParent], node_id: uuid.UUID) -> list[uuid.UUID]:
    """The departments this one could legally be moved under.

    Everything except itself and everything beneath it. The `|` between the two
    sets is a union - "descendants, plus the node itself" - and anything in that
    combined set is excluded.

    Offering an impossible parent and rejecting it afterwards is a worse
    experience than never offering it, so the rule that guards the write is the
    same one that fills the picker.
    """
    forbidden = descendant_ids(nodes, node_id) | {node_id}
    return [node.id for node in nodes if node.id not in forbidden]
