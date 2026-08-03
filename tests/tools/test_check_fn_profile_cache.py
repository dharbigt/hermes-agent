"""The check_fn TTL cache must be scoped per active profile.

Under multiplex, the same check_fn can resolve differently per profile (a flag
set only on one profile). A process-global cache would let a default-profile
verdict poison a named-profile verdict within the TTL, or let a sticky named
True leak the tool into the default profile. The cache key therefore includes
the active profile from the HERMES_SESSION_PROFILE contextvar.
"""

from unittest.mock import patch

import pytest

from tools.registry import _check_fn_cached


@pytest.mark.asyncio
async def test_check_fn_cache_is_profile_scoped(monkeypatch):
    calls = {"kosima": 0, "default": 0}

    def _fn_kosima():
        calls["kosima"] += 1
        return True

    def _fn_default():
        calls["default"] += 1
        return False

    def _ctx(profile: str):
        monkeypatch.setattr(
            "gateway.session_context.get_session_env",
            lambda name, default="": profile if name == "HERMES_SESSION_PROFILE" else default,
        )

    # First call with kosima profile -> True, cached for kosima.
    _ctx("kosima")
    assert _check_fn_cached(_fn_kosima) is True
    # Within TTL, kosima reuses cache (no extra call).
    assert _check_fn_cached(_fn_kosima) is True
    assert calls["kosima"] == 1

    # Switching to default profile must NOT reuse the kosima True verdict.
    _ctx("default")
    assert _check_fn_cached(_fn_default) is False
    assert calls["default"] == 1

    # And kosima still sees its own (unchanged) True verdict.
    _ctx("kosima")
    assert _check_fn_cached(_fn_kosima) is True
    assert calls["kosima"] == 1
