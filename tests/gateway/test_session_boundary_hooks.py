"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = new_session_entry
    runner.session_store.reset_session.return_value = new_session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_fires_finalize_hook(mock_invoke_hook):
    """Regression test for #14981.

    When ``_session_expiry_watcher`` sweeps a session that has aged past
    its reset policy (idle timeout, scheduled reset), it must fire
    ``on_session_finalize`` so plugin providers get the same final-pass
    extraction opportunity they'd get from /new or CLI shutdown.  Before
    the fix, the expiry path evicted the agent but silently skipped the
    hook.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    # The watcher starts with `await asyncio.sleep(0.2)` and loops while
    # `self._running`.  Patch sleep so the 60s initial delay is instant, and
    # make the expiry hook invocation flip `_running` false so the loop
    # exits cleanly after one pass.
    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    def _hook_and_stop(*a, **kw):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    # Look for the finalize call targeting the expired session.
    finalize_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0] and c[0][0] == "on_session_finalize"
    ]
    session_ids = {c[1].get("session_id") for c in finalize_calls}
    assert "sess-expired" in session_ids, (
        f"on_session_finalize was not fired during idle expiry; "
        f"got session_ids={session_ids} (regression of #14981)"
    )


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_clears_last_resolved_model(mock_invoke_hook):
    """Regression test for #58403.

    ``_session_expiry_watcher`` permanently finalizes an expired session and
    already drops ``_session_model_overrides`` / the reasoning override /
    ``_pending_model_notes`` — a resumed conversation must not inherit stale
    per-session state. It missed ``_last_resolved_model``: without clearing
    it, a resumed session could serve a cached model from before it went
    idle on a transient config-cache miss, exactly the #58403 class the
    /new and compression-exhausted-reset paths already guard against.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._last_resolved_model = {
        session_key: "gpt-5",
        "agent:main:telegram:dm:other": "keep-me",
    }

    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    def _hook_and_stop(*a, **kw):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    assert session_key not in runner._last_resolved_model, (
        "session-expiry finalization did not clear the expired session's "
        "_last_resolved_model entry (#58403)"
    )
    assert runner._last_resolved_model["agent:main:telegram:dm:other"] == "keep-me", (
        "session-expiry finalization must only clear the expired session's "
        "own key, not unrelated sessions' cached entries"
    )


@pytest.mark.asyncio
@patch("hermes_cli.lifecycle.invoke_hook")
async def test_agent_reset_without_source_fills_platform_and_chat_id(mock_invoke_hook):
    """Agent reset_session has no MessageEvent source.

    Platform plugins (Keybase clear-history) gate on platform/chat_id from
    on_session_reset. Without falling back to the session entry/session_key,
    Hermes rotates the transcript but skips the Keybase buffer clear.
    """
    from gateway.run import GatewayRunner
    from gateway.slash_commands import GatewaySlashCommandsMixin

    kb_platform = Platform("keybase")
    chat_id = "0000dfc6d100dec4af4803db4f284fc2174f2afa574400f53149cd377fcfca7f"
    session_key = f"agent:kosima:keybase:dm:{chat_id}"
    origin = SessionSource(
        platform=kb_platform,
        user_id="dharbigt",
        chat_id=chat_id,
        user_name="dharbigt",
        chat_type="dm",
        profile="kosima",
    )
    old_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old-kb",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=kb_platform,
        chat_type="dm",
        origin=origin,
        display_name="dharbigt",
    )
    new_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new-kb",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=kb_platform,
        chat_type="dm",
        origin=origin,
        display_name="dharbigt",
    )

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: old_entry}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._invalidate_session_run_generation = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._async_session_store = SimpleNamespace(
        reset_session=AsyncMock(return_value=new_entry),
        _routed=runner.session_store,
    )

    result = await GatewaySlashCommandsMixin._reset_session_by_key(
        runner, session_key, reason="agent_session_reset", source=None
    )
    assert result is new_entry

    reset_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0] and c[0][0] == "on_session_reset"
    ]
    assert reset_calls, "on_session_reset must fire for agent-initiated reset"
    kwargs = reset_calls[-1].kwargs
    assert kwargs.get("platform") == "keybase"
    assert kwargs.get("chat_id") == chat_id
    assert kwargs.get("session_key") == session_key
    assert kwargs.get("new_session_id") == "sess-new-kb"

    emit_platforms = {
        call.args[1].get("platform")
        for call in runner.hooks.emit.await_args_list
        if call.args
    }
    assert "keybase" in emit_platforms


@pytest.mark.asyncio
@patch("hermes_cli.lifecycle.invoke_hook")
async def test_agent_reset_derives_platform_from_multiplex_session_key(
    mock_invoke_hook,
):
    """Even without origin/platform on the entry, multiplex session keys
    carry agent:{profile}:{platform}:{chat_type}:{chat_id}.
    """
    from gateway.run import GatewayRunner
    from gateway.slash_commands import GatewaySlashCommandsMixin

    chat_id = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
    session_key = f"agent:kosima:keybase:dm:{chat_id}"
    old_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        # Intentionally no platform/origin — force session_key parse path.
    )
    new_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: old_entry}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._invalidate_session_run_generation = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._async_session_store = SimpleNamespace(
        reset_session=AsyncMock(return_value=new_entry),
        _routed=runner.session_store,
    )

    await GatewaySlashCommandsMixin._reset_session_by_key(
        runner, session_key, reason="agent_session_reset", source=None
    )
    kwargs = mock_invoke_hook.call_args_list[-1].kwargs
    assert kwargs.get("platform") == "keybase"
    assert kwargs.get("chat_id") == chat_id
