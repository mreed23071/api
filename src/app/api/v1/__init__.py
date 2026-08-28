"""API version 1.

Adding a version is a directory, not a rewrite:

1. copy `v1/` to `v2/`,
2. delete the schemas and routes v2 does not change and re-export them from v1,
3. register the version in `app.api.router.API_VERSIONS`,
4. rename any endpoint function whose *shape* changed (operation ids are global
   - see `app/core/openapi.py`).

v1 stays mounted and keeps working until its sunset date passes.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    accounts,
    connectors,
    ingestion,
    insights,
    messages,
    organization,
    people,
)

API_VERSION = "v1"
VERSION_PREFIX = "/v1"

#: Documentation for the tags this version publishes, surfaced in /docs and in
#: the generated SDK's grouping.
TAGS_METADATA = [
    {
        "name": "ingestion",
        "description": "Cron-triggered pipeline: fetch, filter, embed, store.",
    },
    {
        "name": "insights",
        "description": "Retrieval and agentic summarization of communication history.",
    },
    {
        "name": "directory",
        "description": "People, the external accounts attributed to them, and notes.",
    },
    {
        "name": "messaging",
        "description": "Browsing the retained message corpus.",
    },
    {
        "name": "organization",
        "description": "The department hierarchy and who belongs to it.",
    },
]

router = APIRouter()
router.include_router(ingestion.router)
router.include_router(insights.router)
router.include_router(people.router)
router.include_router(people.notes_router)
router.include_router(accounts.router)
router.include_router(messages.router)
router.include_router(organization.router)
router.include_router(connectors.router)

__all__ = ["API_VERSION", "TAGS_METADATA", "VERSION_PREFIX", "router"]
