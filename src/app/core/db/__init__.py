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
"""

from app.core.db.base import Base, NAMING_CONVENTION
from app.core.db.engine import (
    SessionDep,
    dispose_engine,
    get_engine,
    get_session,
    get_sessionmaker,
)
from app.core.db.repository import Repository
from app.core.db.uow import SessionUnitOfWork

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Repository",
    "SessionDep",
    "SessionUnitOfWork",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
]
