"""Tests for ProfileRoutedSessionStore: per-profile session ownership on disk.

Verifies:
- session keys are routed to the correct profile's SessionStore
- default profile falls back to the global store
- migrate_legacy_global_keys moves agent:<profile>: rows into profile dirs
- aggregation helpers fan out across all sub-stores
"""
import json
import sys
import threading
from pathlib import Path

import pytest

# Ensure the test runs with an isolated HERMES_HOME so profile discovery
# and get_active_profile_name() behave deterministically. Tests isolate
# via tmp_path + monkeypatching hermes_cli.profiles (see each test), not by
# writing a persistent temp dir into the repo root.
import os  # noqa: E402
import tempfile  # noqa: E402


from gateway.session import (  # noqa: E402
    ProfileRoutedSessionStore,
    SessionEntry,
    SessionStore,
    SessionSource,
    build_session_key,
)


def _make_source(platform="keybase", chat_id="conv123", profile=None):
    src = SessionSource(
        platform=__import__("gateway.config", fromlist=["Platform"]).Platform("keybase"),
        chat_id=chat_id,
        chat_name="dharbigt",
        chat_type="dm",
        user_id="dharbigt",
        user_name="dharbigt",
    )
    if profile is not None:
        src.profile = profile
    return src


def _temp_config():
    """Minimal config-like object with sessions_dir + multiplex_profiles."""

    class Cfg:
        sessions_dir = Path(tempfile.mkdtemp(prefix="hermes_routed_test_"))
        multiplex_profiles = True
        write_sessions_json = True
        group_sessions_per_user = True
        thread_sessions_per_user = False

    return Cfg()


def _seed_global_store_with_profile_key(store: SessionStore, profile: str, chat_id: str):
    """Directly write an agent:<profile>: row into the global store's index."""
    key = f"agent:{profile}:keybase:dm:{chat_id}"
    entry = SessionEntry.from_dict({
        "session_key": key,
        "session_id": f"20260101_000000_{chat_id}",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "display_name": "t",
        "platform": "keybase",
        "chat_type": "dm",
        "metadata": {},
        "origin": None,
        "is_fresh_reset": False,
        "was_auto_reset": False,
        "auto_reset_reason": None,
    })
    store._entries[key] = entry
    return key


def test_default_profile_routes_to_global_store():
    cfg = _temp_config()
    global_store = SessionStore(cfg.sessions_dir, cfg)
    routed = ProfileRoutedSessionStore(global_store, cfg)

    src = _make_source(profile="default")
    target = routed._store_for(routed._arg_profile(source=src))
    assert target is global_store


def test_named_profile_builds_own_store(tmp_path):
    cfg = _temp_config()
    # Point the profile's sessions dir at a real temp location under profiles/
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)

    # Patch profile resolution helpers used by _build_profile_store.
    import gateway.session as gs

    orig_dir = gs.get_profile_dir if hasattr(gs, "get_profile_dir") else None

    class FakeProfiles:
        @staticmethod
        def get_profile_dir(name):
            return tmp_path / "profiles" / name

        @staticmethod
        def profile_exists(name):
            return (tmp_path / "profiles" / name).exists()

    # Monkeypatch the lazy imports inside _build_profile_store.
    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = FakeProfiles.get_profile_dir
    hp.profile_exists = FakeProfiles.profile_exists
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        store = routed._store_for("kosima")
        assert store is not global_store
        assert store.sessions_dir == profile_dir / "sessions"
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists


def test_migration_moves_profile_rows_to_profile_store(tmp_path):
    cfg = _temp_config()
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)
    key = _seed_global_store_with_profile_key(global_store, "kosima", "abc")

    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = lambda n: tmp_path / "profiles" / n
    hp.profile_exists = lambda n: (tmp_path / "profiles" / n).exists()
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        moved = routed.migrate_legacy_global_keys()
        assert moved == 1
        # The row is gone from the global store and present in the kosima store.
        assert key not in global_store._entries
        kosima_store = routed._store_for("kosima")
        assert key in kosima_store._entries
        # Idempotent
        assert routed.migrate_legacy_global_keys() == 0
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists


def test_arg_profile_resolves_from_session_key():
    from gateway.session import SessionStore as SS

    assert ProfileRoutedSessionStore._arg_profile(session_key="agent:kosima:keybase:dm:x") == "kosima"
    assert ProfileRoutedSessionStore._arg_profile(session_key="agent:main:keybase:dm:x") == "default"
    assert ProfileRoutedSessionStore._arg_profile() == "default"


def test_get_entry_searches_all_stores(tmp_path):
    cfg = _temp_config()
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)
    key = _seed_global_store_with_profile_key(global_store, "kosima", "zzz")

    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = lambda n: tmp_path / "profiles" / n
    hp.profile_exists = lambda n: (tmp_path / "profiles" / n).exists()
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        routed.migrate_legacy_global_keys()
        # get_entry must find it in the kosima sub-store.
        assert routed.get_entry(key) is not None
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists


def test_state_db_row_migrates_to_profile_db(tmp_path):
    """The state.db mirror row + messages must move into the profile db, and
    be removed from the global db, so the dashboard attributes it correctly."""
    cfg = _temp_config()
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)
    key = _seed_global_store_with_profile_key(global_store, "kosima", "dbrow")
    sid = global_store._entries[key].session_id
    # Write a real session row + message into the global state.db.
    from hermes_state import SessionDB
    gdb = global_store._db
    gdb.create_session(sid, "keybase", session_key=key)
    gdb.replace_messages(sid, [
        {"role": "user", "content": "hello from keybase"},
        {"role": "assistant", "content": "hi there"},
    ])

    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = lambda n: tmp_path / "profiles" / n
    hp.profile_exists = lambda n: (tmp_path / "profiles" / n).exists()
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        moved = routed.migrate_legacy_global_keys()
        assert moved >= 1
        # Global db no longer has the row.
        assert gdb.get_session(sid) is None
        # Profile db has the row + messages.
        kosima_store = routed._store_for("kosima")
        kdb = kosima_store._db
        row = kdb.get_session(sid)
        assert row is not None
        assert row.get("source") == "keybase"
        msgs = kdb.get_messages(sid)
        assert len(msgs) == 2
        # Idempotent: a second migration moves nothing.
        assert routed.migrate_legacy_global_keys() == 0
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists


def test_routed_store_entries_attribute_is_dict_not_function(tmp_path):
    """Regression: ``session_store._entries.get(key)`` must work on the routed
    store (used by the /reset handler). __getattr__ must not return a routing
    closure for the data attribute, which would raise
    'function' object has no attribute 'get'."""
    cfg = _temp_config()
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)
    key = _seed_global_store_with_profile_key(global_store, "kosima", "attr")
    global_store._save()

    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = lambda n: tmp_path / "profiles" / n
    hp.profile_exists = lambda n: (tmp_path / "profiles" / n).exists()
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        # This is exactly what the /reset handler does.
        entries = routed._entries
        assert isinstance(entries, dict)
        assert entries.get(key) is not None
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists

def test_db_for_returns_profile_state_db(tmp_path):
    """Agent persistence must use the profile store's state.db, not global."""
    cfg = _temp_config()
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)

    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = lambda n: tmp_path / "profiles" / n
    hp.profile_exists = lambda n: (tmp_path / "profiles" / n).exists()
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        src = _make_source(profile="kosima")
        key = "agent:kosima:keybase:dm:conv-dbfor"
        pdb = routed.db_for(source=src)
        assert pdb is not None
        assert Path(pdb.db_path) == profile_dir / "state.db"
        assert routed.db_for(session_key=key) is pdb
        # default stays on the global store
        assert routed.db_for(session_key="agent:main:keybase:dm:x") is global_store._db
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists


def test_orphan_prefers_richer_global_transcript(tmp_path):
    """When both DBs share a session_id, keep the side with more messages.

    Regression for the multiplex split where SessionStore wrote a thin shell
    into the profile db while the agent wrote the live transcript into the
    global default state.db. Blind-delete of the global row destroyed history.
    """
    cfg = _temp_config()
    profile_dir = tmp_path / "profiles" / "kosima"
    (profile_dir / "sessions").mkdir(parents=True)
    cfg.sessions_dir = tmp_path / "global"

    global_store = SessionStore(cfg.sessions_dir, cfg)
    key = _seed_global_store_with_profile_key(global_store, "kosima", "rich")
    sid = global_store._entries[key].session_id

    from hermes_state import SessionDB

    # Thin shell already in profile db (as after reset_session).
    import hermes_cli.profiles as hp

    _orig_dir = hp.get_profile_dir
    _orig_exists = hp.profile_exists
    hp.get_profile_dir = lambda n: tmp_path / "profiles" / n
    hp.profile_exists = lambda n: (tmp_path / "profiles" / n).exists()
    try:
        routed = ProfileRoutedSessionStore(global_store, cfg)
        kosima_store = routed._store_for("kosima")
        kdb = kosima_store._db
        kdb.create_session(sid, "keybase", session_key=key, profile_name="kosima")
        kdb.replace_messages(sid, [
            {"role": "user", "content": "old shell"},
        ])

        # Live transcript wrongly landed in global db.
        gdb = global_store._db
        gdb.create_session(sid, "keybase", profile_name="kosima")
        gdb.replace_messages(sid, [
            {"role": "user", "content": "live 1"},
            {"role": "assistant", "content": "live 2"},
            {"role": "user", "content": "live 3"},
        ])

        moved = routed._migrate_orphan_state_db_rows(global_store)
        assert moved >= 1
        assert gdb.get_session(sid) is None
        msgs = kdb.get_messages(sid)
        assert len(msgs) >= 3
        contents = " ".join(str(m.get("content") or "") for m in msgs)
        assert "live 1" in contents
    finally:
        hp.get_profile_dir = _orig_dir
        hp.profile_exists = _orig_exists

