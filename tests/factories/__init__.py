"""Entity builders.

Hand-written rather than generated: a factory that states every default
explicitly is a readable summary of what a valid entity looks like, and it does
not silently drift when a column is added.

Factories build *unpersisted* entities with ids and timestamps already filled
in, so they can be used in unit tests that never touch a database.
"""

from tests.factories.identity import make_relation, make_user
from tests.factories.messaging import make_message, make_raw_message

__all__ = ["make_message", "make_raw_message", "make_relation", "make_user"]
