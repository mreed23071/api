"""The console surface's provisional access rule.

This test exists to make the decision visible rather than to prove much: the
value of `require_console_access` is that closing the surface is one edit, and a
test naming that fact is what stops the call being deleted as pointless.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings, reset_settings_cache
from app.core.errors import AuthenticationError
from app.core.security import provisional
from app.core.security.principal import Principal


@pytest.fixture(autouse=True)
def _clean_settings_cache():  # type: ignore[no-untyped-def]
    """`require_console_access` reads the process-wide settings singleton, so a
    test that changes the flag has to drop the cache on both sides of itself."""
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_the_console_surface_is_open_by_default() -> None:
    """Deliberate, and stated here so nobody has to infer it from silence."""
    assert Settings().console_access_enforced is False
    provisional.require_console_access(Principal.anonymous())


def test_flipping_one_flag_closes_the_whole_surface(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The property that makes the provisional state safe to be in.

    The flag is a setting rather than a module constant, which is the point:
    closing the surface is an environment variable and a restart, not a code
    change, a review and a release.
    """
    monkeypatch.setenv("CONSOLE_ACCESS_ENFORCED", "true")
    reset_settings_cache()
    assert get_settings().console_access_enforced is True

    with pytest.raises(AuthenticationError):
        provisional.require_console_access(Principal.anonymous())


def test_production_refuses_to_boot_with_the_surface_open() -> None:
    """The guard that stops "temporarily open" from shipping.

    A hardcoded constant gave `validate_for_environment` nothing to check. As a
    setting it can be, and is, insisted upon.
    """
    from app.core.errors import ConfigurationError

    settings = Settings(
        app_env="production",
        console_access_enforced=False,
        dev_auth_enabled=False,
        docs_enabled=False,
        cron_token="a-real-generated-secret",
    )

    with pytest.raises(ConfigurationError) as caught:
        settings.validate_for_environment()

    problems = " ".join(caught.value.details["problems"])
    assert "CONSOLE_ACCESS_ENFORCED" in problems
