"""The declarative base shared by every bounded context."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Deterministic constraint names. Without these, Alembic autogenerate produces
#: unstable diffs and `downgrade` cannot find the objects `upgrade` created.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """One `MetaData` for the whole application.

    Contexts are separated by module boundaries and enforced conventions, not by
    separate metadata - they share a database and reference each other by
    foreign key, so a single migration history is the honest model.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
