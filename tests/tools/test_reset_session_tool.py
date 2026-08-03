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


def _write_profile_config(tmp_path, name: str, body: str):
    """Create profiles/<name>/config.yaml under a fake hermes root."""
    profile_home = tmp_path / "profiles" / name
    profile_home.mkdir(parents=True)
    cfg = profile_home / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return profile_home, cfg


def _point_profiles_at(monkeypatch, tmp_path):
    """Make get_profile_dir resolve under tmp_path for both home layouts."""
    monkeypatch.setattr(
        "hermes_cli.profiles._get_default_hermes_home", lambda: tmp_path
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "profiles"
    )


@pytest.mark.asyncio
async def test_check_requirements_defaults_off(monkeypatch, tmp_path):
    """The tool must NOT be available unless explicitly enabled for the profile."""
    _point_profiles_at(monkeypatch, tmp_path)
    _write_profile_config(tmp_path, "kosima", "agent: {}\n")
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    assert reset_session_tool.check_requirements() is False


@pytest.mark.asyncio
async def test_check_requirements_enabled(monkeypatch, tmp_path):
    _point_profiles_at(monkeypatch, tmp_path)
    _write_profile_config(
        tmp_path, "kosima", "agent:\n  allow_agent_session_reset: true\n"
    )
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    assert reset_session_tool.check_requirements() is True


@pytest.mark.asyncio
async def test_check_requirements_reads_profile_config_not_double_nested(
    monkeypatch, tmp_path
):
    """Regression: path must come from get_profile_dir, never
    get_hermes_home()/profiles/<name> (double-nests under the override).
    """
    _point_profiles_at(monkeypatch, tmp_path)
    profile_home, cfg = _write_profile_config(
        tmp_path, "kosima", "agent:\n  allow_agent_session_reset: true\n"
    )
    # Simulate multiplex per-task override: HERMES_HOME already = profile home.
    # A buggy get_hermes_home()/profiles/kosima path would miss the real file.
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: profile_home
    )
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    assert (profile_home / "profiles" / "kosima" / "config.yaml").exists() is False
    assert cfg.is_file()
    assert reset_session_tool.check_requirements() is True


@pytest.mark.asyncio
async def test_check_requirements_works_before_home_override(monkeypatch, tmp_path):
    """Regression: parent/pre-scope layout — HERMES_HOME is the hermes root,
    only HERMES_SESSION_PROFILE names the active profile. Bare read_raw_config()
    would read the base config and miss a profile-only flag.
    """
    _point_profiles_at(monkeypatch, tmp_path)
    _write_profile_config(
        tmp_path, "kosima", "agent:\n  allow_agent_session_reset: true\n"
    )
    # Base config has NO flag.
    (tmp_path / "config.yaml").write_text("agent: {}\n", encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "kosima" if name == "HERMES_SESSION_PROFILE" else default,
    )
    # If the gate wrongly read base config.yaml, this would be False.
    assert reset_session_tool.check_requirements() is True


@pytest.mark.asyncio
async def test_check_requirements_default_profile_ignores_named_flag(
    monkeypatch, tmp_path
):
    """A flag set only on a named profile must not leak into the default profile."""
    _point_profiles_at(monkeypatch, tmp_path)
    _write_profile_config(
        tmp_path, "kosima", "agent:\n  allow_agent_session_reset: true\n"
    )
    # Base/default has no flag.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"agent": {}})

    def _profile_ctx(profile: str):
        monkeypatch.setattr(
            "gateway.session_context.get_session_env",
            lambda name, default="": profile if name == "HERMES_SESSION_PROFILE" else default,
        )

    _profile_ctx("kosima")
    assert reset_session_tool.check_requirements() is True
    _profile_ctx("default")
    assert reset_session_tool.check_requirements() is False


def test_reset_session_is_owned_by_configurable_session_toolset():
    """Orphan guard: platform reverse-mapping only enables CONFIGURABLE toolsets.

    reset_session must live in a configurable toolset whose static membership
    is a subset of hermes-* composites (via _HERMES_CORE_TOOLS). Otherwise the
    gate can be True while get_tool_definitions never offers the tool.
    """
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _get_platform_tools
    from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

    assert "reset_session" in _HERMES_CORE_TOOLS
    assert any(k == "session" for k, _, _ in CONFIGURABLE_TOOLSETS)
    assert resolve_toolset("session", include_registry=False) == ["reset_session"]
    assert "reset_session" in resolve_toolset("hermes-cli", include_registry=False)

    # Default platform config reverse-maps session on for cli/telegram.
    assert "session" in _get_platform_tools({}, "cli")
    assert "session" in _get_platform_tools({}, "telegram")


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
