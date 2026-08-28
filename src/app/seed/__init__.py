"""Demo data: the console's mock dataset, loaded into a real database.

`fixtures.json` is generated from the console's in-memory mock tables, not
written by hand - see `scripts/dump-fixtures.ts` in the console repository. That
matters for a specific reason: the console was built against those exact rows,
so seeding them means the two platforms can be wired together and compared
against a screen somebody has already looked at.

This is demo data and it ships inside the application package so the container
can seed itself with one command. When there is a real deployment it should move
out - a production image has no business carrying eighteen invented people.
Nothing here runs on startup; seeding is always an explicit command.
"""

from app.seed.loader import SeedReport, load_fixtures, seed_database

__all__ = ["SeedReport", "load_fixtures", "seed_database"]
