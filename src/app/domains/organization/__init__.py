"""Organization context - the department hierarchy and who belongs to it.

Owns: the department tree (`org_nodes`) and each person's single membership in
it (`org_node_members`).

Publishes: `OrgNodeView`, `DeletionResult` and the input contracts in `dto.py`,
plus the pure tree arithmetic in `tree.py` - which the authorization evaluator
will use once grants start being scoped to a subtree.

Does not own people. A membership points at a `users` row; who that person is
belongs to the identity context.
"""
