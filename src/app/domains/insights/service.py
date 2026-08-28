"""Retrieval + summarization (Domain 2)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from app.core.errors import NotFoundError
from app.core.pagination import MAX_LIMIT, PageParams, Paginated
from app.core.security.principal import Principal, Scope
from app.core.security.provisional import require_console_access
from app.domains.identity.models import User
from app.domains.identity.service import IdentityService
from app.domains.insights.dto import (
    PersonSummary,
    SummaryWindow,
    UserCommunicationSummary,
    UserSummariesResult,
)
from app.domains.insights.summarization import SummarizationAgent
from app.domains.messaging.dto import MessageFilters
from app.domains.messaging.service import MessageService
from app.domains.uow import UnitOfWork
from app.shared.llm.base import LLMClient, LLMError

logger = logging.getLogger(__name__)

NO_MESSAGES_SUMMARY = "No retained messages for this user yet."


class UserInsightsService:
    """Reads users, loads their recent messages, summarises each concurrently.

    Two properties worth preserving in any rewrite:

    * message loading is **one** query for the whole page (a window function in
      the repository), not one per user;
    * summarisation is concurrent but *bounded* - a page of 100 users must not
      open 100 simultaneous provider connections.
    """

    def __init__(self, uow: UnitOfWork, principal: Principal, llm: LLMClient, settings) -> None:  # type: ignore[no-untyped-def]
        self.uow = uow
        self.principal = principal
        self.llm = llm
        self.settings = settings
        self.identity = IdentityService(uow, principal)
        self.messages = MessageService(uow, principal)
        self.agent = SummarizationAgent(llm, system_prompt=settings.summary_system_prompt)

    async def list_with_summaries(
        self,
        params: PageParams,
        *,
        active_only: bool = True,
        messages_per_user: int = 25,
    ) -> UserSummariesResult:
        """A page of people, each with a freshly generated summary.

        The most sensitive read in the system: it returns real names, email
        addresses, linked accounts, verbatim message text and a generated
        narrative about each person's behaviour - for people who are the
        subjects of the product rather than its users.

        Two queries load the whole page regardless of its size, then every
        summary is generated concurrently under a bounded semaphore.
        """
        self.principal.require(Scope.INSIGHTS_READ, Scope.MESSAGES_READ)

        page = await self.identity.list_users(params, active_only=active_only, with_relations=True)
        messages_by_user = await self.messages.recent_for_users(
            [user.id for user in page.items], per_user_limit=messages_per_user
        )

        semaphore = asyncio.Semaphore(max(1, self.settings.llm_max_concurrency))

        async def build(user: User) -> UserCommunicationSummary:
            """Summarise one person, holding the semaphore while the model runs.

            Defined inside the method so it can close over the semaphore and the
            already-loaded messages. All of these run concurrently, but the
            semaphore caps how many reach the provider at once - a page of a
            hundred people must not open a hundred connections.
            """
            user_messages = messages_by_user.get(user.id, [])
            entry = UserCommunicationSummary(
                user=user,
                relations=list(user.relations),
                recent_messages=user_messages[:5],
                message_count=len(user_messages),
            )

            if not user_messages:
                entry.summary = NO_MESSAGES_SUMMARY
                entry.generated_at = datetime.now(UTC)
                return entry

            async with semaphore:
                try:
                    entry.summary = await self.agent.summarize(user, user_messages)
                    entry.generated_at = datetime.now(UTC)
                except LLMError as exc:
                    # One failing user degrades that entry, never the page.
                    logger.error(
                        "summarization failed",
                        extra={"user_id": str(user.id), "provider": self.agent.provider},
                    )
                    entry.summary_error = str(exc)
            return entry

        entries = await asyncio.gather(*(build(user) for user in page.items))

        return UserSummariesResult(
            page=Paginated(items=list(entries), total=page.total, params=params),
            llm_provider=self.agent.provider,
            llm_model=self.agent.model,
        )

    async def summarize_person(
        self,
        user_id: uuid.UUID,
        window: SummaryWindow | None = None,
        *,
        recent: int = 5,
    ) -> PersonSummary:
        """Summarise one person's history, optionally narrowed to a date window.

        The single-person counterpart to `list_with_summaries`, which does the
        whole page at once. Both call the same agent; this one exists because
        the console's person view asks for one summary at a time and can ask for
        a slice of history rather than all of it.

        Generation is not cached. Every call reaches the language model, which
        is free and deterministic under the stub provider and would not be with
        a real one - persisting summaries against an input fingerprint is a
        deliberate later piece of work, not an oversight here.

        A failure sets `summary_error` and returns normally. One person the
        agent could not describe should not fail the request.
        """
        require_console_access(self.principal)
        window = window or SummaryWindow()

        user = await self.uow.users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.", details={"user_id": str(user_id)})

        # The largest page the shared pagination contract allows. This summary
        # already only *reads* `recent` of these; a person with more history
        # than that in the window gets a summary built from its most recent
        # slice rather than all of it - the same trade-off `browse`'s old
        # unpaged cap made, just expressed in the shared primitive.
        page = await self.messages.browse(
            MessageFilters(
                user_id=user_id,
                sent_from=window.starting,
                sent_to=window.ending,
            ),
            PageParams(limit=MAX_LIMIT),
        )
        messages = list(page.items)

        result = PersonSummary(
            user_id=user_id,
            window=window,
            message_count=len(messages),
            recent_messages=messages[:recent],
            llm_provider=self.agent.provider,
            llm_model=self.agent.model,
        )

        if not messages:
            # Not an error: a person with nothing in the window has nothing to
            # summarise, and saying so is more useful than an empty string.
            result.summary_error = NO_MESSAGES_SUMMARY
            result.generated_at = datetime.now(UTC)
            return result

        try:
            result.summary = await self.agent.summarize(user, messages)
            result.generated_at = datetime.now(UTC)
        except LLMError as exc:
            logger.error(
                "summarization failed",
                extra={"user_id": str(user_id), "provider": self.agent.provider},
            )
            result.summary_error = str(exc)
        return result
