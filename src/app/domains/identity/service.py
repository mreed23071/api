"""Identity business logic."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.core.errors import NotFoundError
from app.core.pagination import PageParams, Paginated
from app.core.security.principal import Principal, Scope
from app.domains.identity.dto import IdentityCandidate, IdentityResolution
from app.domains.identity.models import User, UserRelation
from app.domains.uow import UnitOfWork

logger = logging.getLogger(__name__)

#: Synthesised when a connector reports an identity with no email address. Kept
#: distinct per external id so two anonymous identities never merge into one
#: person by accident.
UNKNOWN_EMAIL_DOMAIN = "unknown.local"


class IdentityService:
    """Resolves people, and provisions them when a connector finds a new one.

    Takes the `Principal` rather than trusting the route to have checked: the
    scope assertion lives next to the behaviour it protects, so the guarantee
    survives the route being reused, wrapped in a worker, or called from a test.
    """

    def __init__(self, uow: UnitOfWork, principal: Principal) -> None:
        self.uow = uow
        self.principal = principal

    async def list_users(
        self,
        params: PageParams,
        *,
        active_only: bool = True,
        with_relations: bool = False,
    ) -> Paginated[User]:
        """One page of people, for the pipeline and the summariser."""
        self.principal.require(Scope.INSIGHTS_READ)
        return await self.uow.users.list_users(
            params, active_only=active_only, with_relations=with_relations
        )

    async def get_user(self, user_id) -> User:  # type: ignore[no-untyped-def]
        """Fetch one person, raising `NotFoundError` rather than returning None."""
        self.principal.require(Scope.INSIGHTS_READ)
        user = await self.uow.users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.", details={"user_id": str(user_id)})
        return user

    async def resolve_or_provision(
        self, candidates: Sequence[IdentityCandidate]
    ) -> IdentityResolution:
        """Map every candidate onto a `UserRelation`, creating what is missing.

        A connector can hand us an identity we have never seen. Rather than drop
        the observation, we provision the user and the relation, keyed on the
        candidate's email so the same person arriving from Slack and from GitHub
        collapses onto one `User`.

        Caller owns the transaction: this method flushes but never commits, so
        provisioning and whatever the caller does next either both land or
        neither does.

        Known weakness, tracked as P-4 in `docs/PROTOTYPE-REPORT.md`: the email
        is an unverified claim from the connector. Before a connector that can
        be influenced by an end user is enabled, this must only auto-merge on
        verified domains.
        """
        self.principal.require(Scope.INGEST_RUN)

        unique: dict[tuple, IdentityCandidate] = {c.key: c for c in candidates}
        resolution = IdentityResolution(
            relations=dict(await self.uow.relations.resolve_many(list(unique.keys())))
        )

        for key, candidate in unique.items():
            if key in resolution.relations:
                continue

            email = candidate.email or f"{candidate.external_id}@{UNKNOWN_EMAIL_DOMAIN}"
            user = await self.uow.users.get_by_email(email)
            if user is None:
                user = await self.uow.users.add(
                    User(
                        email=email,
                        full_name=candidate.display_name or candidate.external_id,
                        display_name=candidate.handle,
                    )
                )
                resolution.users_created += 1
                logger.info(
                    "provisioned user",
                    extra={"user_email": email, "platform": candidate.platform.value},
                )

            relation = await self.uow.relations.add(
                UserRelation(
                    user_id=user.id,
                    platform=candidate.platform,
                    external_id=candidate.external_id,
                    external_handle=candidate.handle,
                    external_email=candidate.email,
                    details={"provisioned_by": "ingestion"},
                )
            )
            resolution.relations[key] = relation
            resolution.relations_created += 1

        return resolution
