"""The connectors' mapping logic - every other ingestion test leans on this.

No fixtures-service runs here. `httpx.MockTransport` answers each request with
a canned payload in that platform's *own* wire shape, so what is under test is
the translation into `RawMessage`: field renames, timestamp parsing, and where
each platform buries the author. A payload shaped like ours already would
prove nothing - these are shaped the way Slack, Graph and GitHub actually
answer.
"""

from __future__ import annotations

import httpx
import pytest

from app.domains.identity.models import Platform
from app.domains.ingestion.sources import (
    GitHubSource,
    MessageSource,
    SlackSource,
    SourceError,
    TeamsSource,
)


def transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def json_response(payload) -> httpx.Response:  # type: ignore[no-untyped-def]
    return httpx.Response(200, json=payload)


# -- Slack --------------------------------------------------------------


def slack_payload(*messages: dict) -> dict:  # type: ignore[type-arg]
    return {"ok": True, "messages": list(messages), "has_more": False}


def slack_message(**overrides) -> dict:  # type: ignore[type-arg]
    base = {
        "type": "message",
        "user": "U-ALICE",
        "text": "Staging deploy is green.",
        "ts": "1724825930.000200",
        "channel": "C0THREADGEN",
        "user_profile": {
            "real_name": "Alice Nguyen",
            "display_name": "alice",
            "email": "alice@example.com",
        },
    }
    return {**base, **overrides}


def slack(handler) -> SlackSource:  # type: ignore[no-untyped-def]
    return SlackSource(transport=transport(handler))


def test_slack_source_satisfies_the_port() -> None:
    assert isinstance(slack(lambda r: json_response(slack_payload())), MessageSource)


async def test_slack_requests_the_right_path_and_forwards_count() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["count"] = request.url.params.get("count")
        return json_response(slack_payload())

    await slack(handler).fetch(limit=5)
    assert seen["path"] == "/slack/messages"
    assert seen["count"] == "5"


async def test_slack_maps_the_message_shape() -> None:
    messages = await slack(lambda r: json_response(slack_payload(slack_message()))).fetch()
    message = messages[0]

    assert message.platform is Platform.SLACK
    assert message.external_message_id == "C0THREADGEN-1724825930.000200"
    assert message.external_author_id == "U-ALICE"
    assert message.author_handle == "alice"
    assert message.author_email == "alice@example.com"
    assert message.author_display_name == "Alice Nguyen"
    assert message.conversation_id == "C0THREADGEN"
    assert message.content == "Staging deploy is green."
    assert message.kind == "message"


async def test_slack_ts_is_parsed_as_epoch_seconds() -> None:
    message = (await slack(lambda r: json_response(slack_payload(slack_message()))).fetch())[0]
    assert message.sent_at.year == 2024
    assert message.sent_at.tzinfo is not None


async def test_slack_ids_are_unique_and_stable_across_calls() -> None:
    payload = slack_payload(slack_message(ts="1.1"), slack_message(ts="2.2", user="U-BEN"))
    first = await slack(lambda r: json_response(payload)).fetch()
    second = await slack(lambda r: json_response(payload)).fetch()
    assert [m.key for m in first] == [m.key for m in second]
    assert len({m.key for m in first}) == 2


async def test_a_slack_transport_failure_is_a_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(SourceError, match="fixtures-service request"):
        await slack(handler).fetch()


async def test_a_slack_http_error_is_a_source_error() -> None:
    with pytest.raises(SourceError, match="fixtures-service request"):
        await slack(lambda r: httpx.Response(503)).fetch()


# -- Teams ----------------------------------------------------------------


def teams_payload(*items: dict) -> dict:  # type: ignore[type-arg]
    return {"value": list(items)}


def teams_message(**overrides) -> dict:  # type: ignore[type-arg]
    base = {
        "id": "1724825930000",
        "createdDateTime": "2026-08-27T21:38:30.893000Z",
        "from": {
            "user": {
                "id": "U-BEN",
                "displayName": "Ben Hartley",
                "userPrincipalName": "ben.hartley@threadline.example",
                "email": "ben@example.com",
            }
        },
        "body": {"contentType": "text", "content": "Onboarding session is tomorrow at 10."},
        "channelIdentity": {"channelId": "19:threadline-general@thread.tacv2"},
    }
    return {**base, **overrides}


def teams(handler) -> TeamsSource:  # type: ignore[no-untyped-def]
    return TeamsSource(transport=transport(handler))


def test_teams_source_satisfies_the_port() -> None:
    assert isinstance(teams(lambda r: json_response(teams_payload())), MessageSource)


async def test_teams_maps_the_chat_message_shape() -> None:
    item = teams_message()
    message = (await teams(lambda r: json_response(teams_payload(item))).fetch())[0]

    assert message.platform is Platform.TEAMS
    assert message.external_message_id == "1724825930000"
    assert message.external_author_id == "U-BEN"
    assert message.author_handle == "ben.hartley@threadline.example"
    assert message.author_email == "ben@example.com"
    assert message.author_display_name == "Ben Hartley"
    assert message.conversation_id == "19:threadline-general@thread.tacv2"
    assert message.content == "Onboarding session is tomorrow at 10."


async def test_teams_created_date_time_is_real_iso8601() -> None:
    item = teams_message()
    message = (await teams(lambda r: json_response(teams_payload(item))).fetch())[0]
    assert message.sent_at.year == 2026 and message.sent_at.month == 8


async def test_an_unsupported_teams_content_type_is_a_source_error() -> None:
    item = teams_message(body={"contentType": "html", "content": "<p>hi</p>"})
    with pytest.raises(SourceError, match="contentType"):
        await teams(lambda r: json_response(teams_payload(item))).fetch()


# -- GitHub -----------------------------------------------------------------


def commit(**overrides) -> dict:  # type: ignore[type-arg]
    base = {
        "sha": "3691593a7c1e4f0b9d2a6c8e1f4b7d0a2c5e8f1b",
        "commit": {
            "author": {
                "name": "Ben Hartley",
                "email": "ben@example.com",
                "date": "2026-08-27T21:38:30Z",
            },
            "message": "Add migration for the messages embedding column",
        },
        "author": {"login": "benh"},
        "html_url": "https://github.com/threadline/api/commit/3691593a7c1e4f0b9d2a6c8e1f4b7d0a2c5e8f1b",
        "files": [
            {
                "filename": "migrations/versions/0002_add_embedding.py",
                "status": "added",
                "additions": 42,
                "deletions": 0,
            }
        ],
        "stats": {"additions": 42, "deletions": 0},
        "repository": "threadline/api",
        "branch": "main",
    }
    return {**base, **overrides}


def github(handler) -> GitHubSource:  # type: ignore[no-untyped-def]
    return GitHubSource(transport=transport(handler))


def test_github_source_satisfies_the_port() -> None:
    assert isinstance(github(lambda r: json_response([])), MessageSource)


async def test_github_maps_commits_not_chat_messages() -> None:
    message = (await github(lambda r: json_response([commit()])).fetch())[0]

    assert message.platform is Platform.GITHUB
    assert message.kind == "commit"
    assert message.external_message_id == "threadline/api@3691593"
    assert message.author_handle == "benh"
    assert message.author_email == "ben@example.com"
    assert message.author_display_name == "Ben Hartley"
    assert message.conversation_id == "threadline/api"
    assert message.content == "Add migration for the messages embedding column"


async def test_github_content_is_the_commit_message_not_a_diff() -> None:
    """The filtering agent reads `content` - it should see the commit message,
    not the whole payload."""
    message = (await github(lambda r: json_response([commit()])).fetch())[0]
    assert "sha" not in message.content.lower()


async def test_github_commit_metadata_matches_what_the_read_side_expects() -> None:
    """CommitDetail/CommitFile in api/v1/schemas/messaging.py is what this
    shape has to match for the console to render it."""
    message = (await github(lambda r: json_response([commit()])).fetch())[0]
    detail = message.metadata["commit"]
    assert detail["sha"] and detail["repository"] == "threadline/api" and detail["branch"] == "main"
    assert isinstance(detail["files"], list) and detail["files"]
    for f in detail["files"]:
        assert f["path"] and f["status"] in {"added", "modified", "removed"}


async def test_github_prefers_the_git_commit_email_over_the_github_account() -> None:
    """GitHub never exposes an email on the account object - only the git
    commit's author line has one, which is what a connector provisions
    identity against."""
    item = commit(author={"login": "benh"})
    message = (await github(lambda r: json_response([item])).fetch())[0]
    assert message.author_email == "ben@example.com"


async def test_github_falls_back_to_the_commit_email_with_no_matched_account() -> None:
    """A commit whose author has no linked GitHub account still has a git
    identity worth provisioning against."""
    item = commit(author=None)
    message = (await github(lambda r: json_response([item])).fetch())[0]
    assert message.external_author_id == "ben@example.com"
    assert message.author_handle is None


async def test_github_ids_are_unique_and_stable_across_calls() -> None:
    payload = [
        commit(sha="a" * 40, repository="threadline/api"),
        commit(sha="b" * 40, repository="threadline/infra"),
    ]
    first = await github(lambda r: json_response(payload)).fetch()
    second = await github(lambda r: json_response(payload)).fetch()
    assert [m.key for m in first] == [m.key for m in second]
    assert len({m.key for m in first}) == 2
