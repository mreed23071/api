"""Persistence infrastructure.

Split by responsibility so nothing imports more than it needs:

* `base`       - the declarative `Base` and its naming convention
* `mixins`     - reusable mapped-column mixins
* `engine`     - the process-wide engine, sessionmaker and session dependency
* `repository` - the repository base class and the tenant-scoping seam
* `uow`        - transaction control

Note the direction of dependencies: nothing here imports `app.domains` or
`app.api`. Concrete repositories live in their bounded context; the object that
aggregates them lives in `app.domains.uow`.

**`engine` is deliberately not re-exported here.** This package's `__init__`
used to pull `engine` in eagerly, which meant importing anything that touched
`Base` - every mapped model, and through them `app.workflows.dto` - also
imported the engine module and its settings. That transitive edge is what kept
the ingestion workflow from running inside Temporal's sandbox: workflow code
must be able to import its own DTOs without dragging database machinery in
behind them. Import `app.core.db.engine` by its full path when you want it;
every call site in this codebase already does.
"""

from app.core.db.base import NAMING_CONVENTION, Base
from app.core.db.repository import Repository
from app.core.db.uow import SessionUnitOfWork

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Repository",
    "SessionUnitOfWork",
]
