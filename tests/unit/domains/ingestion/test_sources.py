"""The mock connector, which every other ingestion test leans on."""

from __future__ import annotations

from app.domains.ingestion.sources import MessageSource, MockMessageService


def test_it_satisfies_the_port() -> None:
    assert isinstance(MockMessageService(), MessageSource)


async def test_fetch_is_deterministic() -> None:
    first = await MockMessageService().fetch()
    second = await MockMessageService().fetch()
    assert [m.external_message_id for m in first] == [m.external_message_id for m in second]


async def test_external_ids_are_unique() -> None:
    messages = await MockMessageService().fetch()
    assert len({m.key for m in messages}) == len(messages)


async def test_limit_is_honoured() -> None:
    assert len(await MockMessageService().fetch(limit=3)) == 3


async def test_fixture_mixes_business_and_personal_content() -> None:
    """Otherwise the filtering tests would prove nothing."""
    messages = await MockMessageService().fetch()
    joined = " ".join(m.content.lower() for m in messages)
    assert "deploy" in joined and "birthday" in joined


async def test_identity_candidates_carry_enough_to_provision_a_user() -> None:
    for message in await MockMessageService().fetch():
        candidate = message.as_identity_candidate()
        assert candidate.external_id and candidate.email and candidate.display_name
