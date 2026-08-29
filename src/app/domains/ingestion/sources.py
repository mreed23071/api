"""Message source connectors.

`MessageSource` is the port. A real Slack/GitHub/Teams connector implements
this protocol and is swapped into `_SOURCES` at the bottom of this module -
the pipeline itself never changes. The registry is the single place a real
connector plugs in, and both the API and the ingestion worker resolve
through it.

Every connector here talks to `fixtures-service` - a small separate process
(`bootstrap/fixtures-service/`) that answers in each platform's *own* wire
shape: Slack's `conversations.history` shape, Microsoft Graph's `chatMessage`
shape, GitHub's commits-API shape. None of it looks like `RawMessage`. The
translation below - field renames, timestamp parsing, digging an author out
of wherever that platform buries it - is what a real connector's mapping code
actually looks like; it is the reason this module exists rather than
`RawMessage(...)` being constructed inline wherever a message is needed.

What a real connector will additionally need, and this one deliberately does
not model: an incremental cursor (a watermark per channel or per repository)
so each run fetches only what is new. That belongs on this protocol as a
`fetch(since: Cursor)` argument, plus a table to persist the cursor.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.domains.identity.models import Platform
from app.domains.ingestion.dto import RawMessage


class SourceError(RuntimeError):
    """Raised when a connector cannot reach or make sense of its platform."""


@runtime_checkable
class MessageSource(Protocol):
    """Port every connector implements."""

    name: str

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        """Pull recent messages from the platform, newest first.

        The `...` body is the whole point of a Protocol: this declares the shape
        a connector must have without providing any behaviour. A real Slack or
        GitHub connector satisfies it simply by having a matching method - there
        is nothing to inherit and nothing to register.
        """
        ...


def _client(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    """One short-lived client per fetch - these run once per ingestion call,
    not per request, so pooling a client across calls buys nothing.

    `transport` is injected only by tests, which supply a `MockTransport` so
    the mapping logic can be asserted without a live fixtures-service.
    """
    return httpx.AsyncClient(
        base_url=settings.fixtures_service_url,
        timeout=settings.fixtures_timeout_seconds,
        transport=transport,
    )


async def _get(client: httpx.AsyncClient, path: str, *, count: int | None) -> Any:
    try:
        response = await client.get(path, params={"count": count} if count else None)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"fixtures-service request to {path} failed: {exc}") from exc
    return response.json()


class SlackSource:
    """Talks to `GET /slack/messages` and maps Slack's own message shape.

    Slack identifies the author only by an opaque `user` id in the message
    itself - a real connector resolves the profile with a separate
    `users.info` call per author; this fixture folds that enrichment into
    `user_profile` so one request is enough. `ts` is Slack's actual wire
    format: a stringified Unix timestamp with a fractional part.
    """

    name = "slack"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        async with _client(self._settings, self._transport) as client:
            payload = await _get(client, "/slack/messages", count=limit)

        messages = payload.get("messages", [])
        return [self._map(message) for message in messages]

    def _map(self, message: dict[str, Any]) -> RawMessage:
        profile = message.get("user_profile") or {}
        return RawMessage(
            external_message_id=f"{message['channel']}-{message['ts']}",
            platform=Platform.SLACK,
            external_author_id=message["user"],
            author_handle=profile.get("display_name"),
            author_email=profile.get("email"),
            author_display_name=profile.get("real_name"),
            conversation_id=message["channel"],
            content=message["text"],
            # Slack's `ts` is seconds since epoch, as a string.
            sent_at=datetime.fromtimestamp(float(message["ts"]), tz=UTC),
            metadata={"source": self.name, "slack_ts": message["ts"]},
        )


class TeamsSource:
    """Talks to `GET /teams/messages` and maps a Microsoft Graph `chatMessage`.

    Graph nests the author three levels down (`from.user`) and the text one
    level down (`body.content`, alongside a `contentType` a real connector
    would check before trusting it as plain text). `createdDateTime` is real
    ISO 8601, unlike Slack's epoch string - two platforms, two conventions,
    which is exactly the kind of thing a shared `MockChatSource` used to hide.
    """

    name = "teams"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        async with _client(self._settings, self._transport) as client:
            payload = await _get(client, "/teams/messages", count=limit)

        return [self._map(item) for item in payload.get("value", [])]

    def _map(self, item: dict[str, Any]) -> RawMessage:
        author = item["from"]["user"]
        body = item["body"]
        if body.get("contentType") != "text":
            # A real connector would strip HTML here; every fixture message is
            # already plain text, so there is nothing further to do but not
            # silently trust a content type we haven't handled.
            raise SourceError(f"unsupported Teams body contentType: {body.get('contentType')!r}")
        return RawMessage(
            external_message_id=item["id"],
            platform=Platform.TEAMS,
            external_author_id=author["id"],
            author_handle=author.get("userPrincipalName"),
            author_email=author.get("email"),
            author_display_name=author.get("displayName"),
            conversation_id=item["channelIdentity"]["channelId"],
            content=body["content"],
            sent_at=datetime.fromisoformat(item["createdDateTime"].replace("Z", "+00:00")),
            metadata={"source": self.name, "teams_message_id": item["id"]},
        )


class GitHubSource:
    """Talks to `GET /github/commits` and maps GitHub's commits-API shape.

    GitHub splits identity in two: `commit.author` is the git commit's author
    line (name + email, whatever was in the committer's local git config) and
    the top-level `author` is the matched GitHub account (only ever a
    `login`, no email - GitHub does not expose it). A real connector has to
    reconcile both; this one prefers the git commit's email since that is the
    one worth provisioning an identity against.
    """

    name = "github"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport

    async def fetch(self, *, limit: int | None = None) -> Sequence[RawMessage]:
        async with _client(self._settings, self._transport) as client:
            commits = await _get(client, "/github/commits", count=limit)

        return [self._map(commit) for commit in commits]

    def _map(self, commit: dict[str, Any]) -> RawMessage:
        git_author = commit["commit"]["author"]
        github_account = commit.get("author") or {}
        stats = commit.get("stats", {})
        return RawMessage(
            external_message_id=f"{commit['repository']}@{commit['sha'][:7]}",
            platform=Platform.GITHUB,
            # The GitHub login is the stabler identifier across commits (an
            # author's git config email can vary machine to machine); the git
            # commit's own name/email fill in the profile.
            external_author_id=github_account.get("login", git_author["email"]),
            author_handle=github_account.get("login"),
            author_email=git_author.get("email"),
            author_display_name=git_author.get("name"),
            conversation_id=commit["repository"],
            content=commit["commit"]["message"],
            sent_at=datetime.fromisoformat(git_author["date"].replace("Z", "+00:00")),
            kind="commit",
            metadata={
                "source": self.name,
                "commit": {
                    "sha": commit["sha"],
                    "repository": commit["repository"],
                    "branch": commit["branch"],
                    "url": commit["html_url"],
                    "files": [
                        {
                            "path": f["filename"],
                            "status": f["status"],
                            "additions": f["additions"],
                            "deletions": f["deletions"],
                        }
                        for f in commit.get("files", [])
                    ],
                    "additions": stats.get("additions", 0),
                    "deletions": stats.get("deletions", 0),
                },
            },
        )


#: One connector per platform, not one pipeline for all of them - each entry is
#: swapped for a real connector independently, on its own schedule, without
#: touching the others. `Callable[[], MessageSource]`, not an instance: a fresh
#: connector per call, since a real one will hold a session or a cursor.
#:
#: Lives in the domain rather than in `api/deps` because the API is not the
#: only caller: the ingestion worker resolves connectors too, and a worker that
#: had to import the HTTP layer to find one would have the dependency arrow
#: backwards.
_SOURCES: dict[Platform, Callable[[], MessageSource]] = {
    Platform.GITHUB: GitHubSource,
    Platform.SLACK: SlackSource,
    Platform.TEAMS: TeamsSource,
}


def source_for(platform: Platform) -> MessageSource:
    """The connector one platform's ingestion pipeline pulls from.

    Raises `NotFoundError` for a platform with no connector configured yet
    (email, linear, other), so the edge answers a clean 404 rather than
    failing somewhere deeper.
    """
    try:
        return _SOURCES[platform]()
    except KeyError:
        raise NotFoundError(
            f"No connector configured for platform '{platform.value}'.",
            details={"platform": platform.value},
        ) from None


def configured_platforms() -> list[Platform]:
    """Platforms that actually have a connector, in declaration order."""
    return list(_SOURCES)
