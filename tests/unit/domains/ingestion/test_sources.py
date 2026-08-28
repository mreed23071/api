"""The mock connectors, which every other ingestion test leans on."""

from __future__ import annotations

from app.domains.identity.models import Platform
from app.domains.ingestion.sources import MessageSource, MockChatSource, MockGitHubCommitSource


def slack() -> MockChatSource:
    return MockChatSource(Platform.SLACK, name="slack-mock")


def teams() -> MockChatSource:
    return MockChatSource(Platform.TEAMS, name="teams-mock")


def github() -> MockGitHubCommitSource:
    return MockGitHubCommitSource()


# -- MockChatSource (Slack, Teams) ------------------------------------------


def test_it_satisfies_the_port() -> None:
    assert isinstance(slack(), MessageSource)
    assert isinstance(teams(), MessageSource)


async def test_fetch_is_deterministic() -> None:
    first = await slack().fetch()
    second = await slack().fetch()
    assert [m.external_message_id for m in first] == [m.external_message_id for m in second]


async def test_external_ids_are_unique() -> None:
    messages = await slack().fetch()
    assert len({m.key for m in messages}) == len(messages)


async def test_limit_is_honoured() -> None:
    assert len(await slack().fetch(limit=1)) == 1


async def test_only_that_platforms_messages_come_back() -> None:
    for message in await slack().fetch():
        assert message.platform == Platform.SLACK
    for message in await teams().fetch():
        assert message.platform == Platform.TEAMS


async def test_chat_messages_are_never_marked_as_commits() -> None:
    for message in await slack().fetch():
        assert message.kind == "message"


async def test_fixture_mixes_business_and_personal_content() -> None:
    """Otherwise the filtering tests would prove nothing."""
    messages = await slack().fetch()
    joined = " ".join(m.content.lower() for m in messages)
    assert "deploy" in joined and "birthday" in joined


async def test_identity_candidates_carry_enough_to_provision_a_user() -> None:
    for message in await slack().fetch():
        candidate = message.as_identity_candidate()
        assert candidate.external_id and candidate.email and candidate.display_name


# -- MockGitHubCommitSource ---------------------------------------------------


def test_github_source_satisfies_the_port() -> None:
    assert isinstance(github(), MessageSource)


async def test_github_fetch_is_deterministic() -> None:
    first = await github().fetch()
    second = await github().fetch()
    assert [m.external_message_id for m in first] == [m.external_message_id for m in second]


async def test_commits_are_marked_with_kind_commit() -> None:
    for message in await github().fetch():
        assert message.platform == Platform.GITHUB
        assert message.kind == "commit"


async def test_commit_metadata_matches_what_the_read_side_expects() -> None:
    """CommitDetail/CommitFile in api/v1/schemas/messaging.py is what this
    shape has to match for the console to render it."""
    for message in await github().fetch():
        commit = message.metadata["commit"]
        assert commit["sha"] and commit["repository"]
        assert isinstance(commit["files"], list) and commit["files"]
        for file in commit["files"]:
            assert file["path"] and file["status"] in {"added", "modified", "removed"}


async def test_commit_content_is_the_commit_message_not_a_diff() -> None:
    """The filtering agent reads `content` - it should see the commit message,
    not the whole payload."""
    for message in await github().fetch():
        assert message.content
        assert "sha" not in message.content.lower()
