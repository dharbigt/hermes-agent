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
    """The tool must NOT be available unless explicitly enabled."""

    def _cfg_off():
        return {"agent": {}}

    monkeypatch.setattr("hermes_cli.config.load_config", _cfg_off)
    assert reset_session_tool.check_requirements() is False


@pytest.mark.asyncio
async def test_check_requirements_enabled(monkeypatch):
    def _cfg_on():
        return {"agent": {"allow_agent_session_reset": True}}

    monkeypatch.setattr("hermes_cli.config.load_config", _cfg_on)
    assert reset_session_tool.check_requirements() is True


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
