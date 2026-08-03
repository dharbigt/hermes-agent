"""Tests for the agent-callable reset_session tool.

Covers the config gate (off by default) and the happy-path behavior: the
handler reaches the gateway runner and calls the shared
``_reset_session_by_key`` teardown, returning the new session id as JSON.
"""

import json

import pytest

from tools import reset_session_tool


def _fake_entry(session_id: str):
    class _E:
        pass

    e = _E()
    e.session_id = session_id
    return e


@pytest.mark.asyncio
async def test_check_requirements_defaults_off(monkeypatch):
    """The tool must NOT be available unless explicitly enabled for the profile."""

    def _cfg_off():
        return {"agent": {}}

    monkeypatch.setattr("hermes_cli.config.read_raw_config", _cfg_off)
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    assert reset_session_tool.check_requirements() is False


@pytest.mark.asyncio
async def test_check_requirements_enabled(monkeypatch):
    def _cfg_on():
        return {"agent": {"allow_agent_session_reset": True}}

    monkeypatch.setattr("hermes_cli.config.read_raw_config", _cfg_on)
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    assert reset_session_tool.check_requirements() is True


@pytest.mark.asyncio
async def test_check_requirements_reads_profile_config_not_double_nested(monkeypatch):
    """Regression: under multiplex the per-task HERMES_HOME override already
    points at the profile home, so the named-profile read must resolve to the
    profile's own config.yaml and NOT a double-nested
    profiles/<name>/profiles/<name>/config.yaml.
    """
    captured = {}

    def _cfg_on():
        return {"agent": {"allow_agent_session_reset": True}}

    # read_raw_config() is profile-aware (no path arg): it reads get_config_path(),
    # which under the override resolves to the profile home. Spy on the call to
    # ensure it is invoked with no path (i.e. we rely on get_config_path, never
    # construct a nested path ourselves).
    def _spy_read_raw():
        captured["called"] = True
        return _cfg_on()

    monkeypatch.setattr("hermes_cli.config.read_raw_config", _spy_read_raw)
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    assert reset_session_tool.check_requirements() is True
    assert captured.get("called") is True


@pytest.mark.asyncio
async def test_check_requirements_default_profile_ignores_named_flag(monkeypatch):
    """A flag set only on a named profile must not leak into the default profile.

    The default profile reads load_config() (base config); a named profile
    reads its own profile config via read_raw_config(). Scoping the gate by
    profile means a kosima-only flag stays kosima-only.
    """
    # Base config (default profile) has NO flag; kosima profile config HAS it.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"agent": {}})
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"agent": {"allow_agent_session_reset": True}},
    )

    def _profile_ctx(profile: str):
        monkeypatch.setattr(
            "gateway.session_context.get_session_env",
            lambda name, default="": profile if name == "HERMES_SESSION_PROFILE" else default,
        )

    _profile_ctx("kosima")
    assert reset_session_tool.check_requirements() is True
    _profile_ctx("default")
    assert reset_session_tool.check_requirements() is False


@pytest.mark.asyncio
async def test_handler_resets_via_runner(monkeypatch):
    """Handler drives the shared teardown and returns the new session id."""
    sentinel = _fake_entry("brand-new-sid")
    captured = {}

    class _FakeRunner:
        async def _reset_session_by_key(self, session_key, *, reason="agent_session_reset", source=None):
            captured["session_key"] = session_key
            captured["reason"] = reason
            return sentinel

    monkeypatch.setattr(
        "gateway.run._gateway_runner_ref", lambda: _FakeRunner()
    )
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name: "agent:kosima:keybase:dm:abc" if name == "HERMES_SESSION_KEY" else "",
    )

    result = json.loads(await reset_session_tool.reset_session_tool())
    assert result["success"] is True
    assert result["new_session_id"] == "brand-new-sid"
    assert captured["session_key"] == "agent:kosima:keybase:dm:abc"
    assert captured["reason"] == "agent_session_reset"


@pytest.mark.asyncio
async def test_handler_no_runner(monkeypatch):
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    result = json.loads(await reset_session_tool.reset_session_tool())
    assert result["success"] is False
    assert "gateway runner" in result["error"]


@pytest.mark.asyncio
async def test_handler_no_session_key(monkeypatch):
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: object())
    monkeypatch.setattr(
        "gateway.session_context.get_session_env", lambda name: ""
    )
    result = json.loads(await reset_session_tool.reset_session_tool())
    assert result["success"] is False
    assert "session key" in result["error"]
