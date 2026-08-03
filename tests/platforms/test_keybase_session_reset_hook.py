"""Tests for the Keybase ``on_session_reset`` plugin hook.

The hook must, on a session reset:
  1. clear the visible Keybase chat history for the reset conversation,
  2. drop in-memory send/typing buffer state, and
  3. post a fresh unprompted salutatory message (so the wiped chat reads as a
     new session).

Under multiplex the gateway may call the hook without a ``source``; the chat id
is carried on the hook payload (and resolved from the session_key). The hook
must still reach the right conversation and greet it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms import keybase as kb


def _install_fake_adapter(monkeypatch, *, home_channels=("kosima",), greeting=""):
    inst = SimpleNamespace(
        home_channels=list(home_channels),
        greeting_after_reset=greeting,
        _recent_sent_ids={"stale1", "stale2"},
        clear_history=AsyncMock(return_value=True),
        send=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
    )
    monkeypatch.setattr(kb, "_ACTIVE_INSTANCE", inst)
    return inst


@pytest.mark.asyncio
async def test_hook_clears_history_and_greets_reset_conversation(monkeypatch):
    inst = _install_fake_adapter(monkeypatch)

    kb._on_keybase_session_reset(
        platform="keybase",
        chat_id="CONV123",
        session_key="agent:kosima:keybase:dm:CONV123",
    )
    # let the scheduled coroutine run on the running loop
    await asyncio.sleep(0.05)

    inst.clear_history.assert_awaited_once_with("CONV123")
    assert inst._recent_sent_ids == set()
    inst.send.assert_awaited_once()
    target, greeting = inst.send.await_args.args
    assert target == "CONV123"
    assert "New session started" in greeting


@pytest.mark.asyncio
async def test_hook_falls_back_to_home_channel_without_chat_id(monkeypatch):
    inst = _install_fake_adapter(monkeypatch, home_channels=("kosima",))

    kb._on_keybase_session_reset(platform="keybase", session_key="agent:kosima:keybase:dm:CONVX")
    await asyncio.sleep(0.05)

    # No chat_id -> greet the first home channel.
    target, _ = inst.send.await_args.args
    assert target == "kosima"


@pytest.mark.asyncio
async def test_hook_uses_configured_greeting(monkeypatch):
    inst = _install_fake_adapter(monkeypatch, greeting="Fresh start, boss.")

    kb._on_keybase_session_reset(platform="keybase", chat_id="CONV1")
    await asyncio.sleep(0.05)

    _, greeting = inst.send.await_args.args
    assert greeting == "Fresh start, boss."


@pytest.mark.asyncio
async def test_hook_ignores_other_platforms(monkeypatch):
    inst = _install_fake_adapter(monkeypatch)

    kb._on_keybase_session_reset(platform="telegram", chat_id="CONV1")
    await asyncio.sleep(0.05)

    inst.clear_history.assert_not_awaited()
    inst.send.assert_not_awaited()
