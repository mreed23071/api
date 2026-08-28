"""API-suite aliases.

The `app`, `client`, `seeded_uow` and `embeddings` fixtures live in the root
conftest so the contract suite can use them too. This module only re-exports the
header constants the API tests read against.
"""

from tests.conftest import (  # noqa: F401
    ADMIN_HEADERS,
    INGEST_HEADERS,
    READER_HEADERS,
)
