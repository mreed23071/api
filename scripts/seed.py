"""Load the demo dataset into the configured database.

    make seed

Safe to run repeatedly: rows are keyed by deterministic ids, so a second run
inserts nothing. Never runs on startup - seeding an application into existence
is something a person should have to ask for.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.db.engine import dispose_engine, get_sessionmaker
from app.seed.loader import seed_database, summarise


async def main() -> int:
    """Open one session, seed inside one transaction, report what happened.

    The whole load is a single transaction: a partial seed - people without
    their messages, departments without their members - would be worse than no
    seed at all, because it looks like it worked.
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            async with session.begin():
                report = await seed_database(session)
        print(summarise(report))
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
