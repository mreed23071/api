"""The console surface's provisional access rule.

This test exists to make the decision visible rather than to prove much: the
value of `require_console_access` is that closing the surface is one edit, and a
test naming that fact is what stops the call being deleted as pointless.
"""

from __future__ import annotations

import pytest

from app.core.errors import AuthenticationError
from app.core.security import provisional
from app.core.security.principal import Principal


def test_the_console_surface_is_open_by_default() -> None:
    """Deliberate, and stated here so nobody has to infer it from silence."""
    assert provisional.ENFORCED is False
    provisional.require_console_access(Principal.anonymous())


def test_flipping_one_flag_closes_the_whole_surface(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The property that makes the provisional state safe to be in."""
    monkeypatch.setattr(provisional, "ENFORCED", True)

    with pytest.raises(AuthenticationError):
        provisional.require_console_access(Principal.anonymous())
