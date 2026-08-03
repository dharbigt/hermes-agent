"""Agent-callable session reset tool.

Exposes a ``reset_session`` tool the model can invoke to start a fresh
conversation — the agent-side equivalent of a user typing ``/reset``. It runs
the *exact same* teardown path as ``/reset`` (``GatewayRunner._reset_session_by_key``):
invalidate the run generation, release the running-agent slot, clean up the
old agent's resources, clear conversation-scoped state, interrupt in-flight
delegations, rotate the session id, and fire the lifecycle hooks.

This is a **service-gated** tool: it is only offered to the model when
``agent.allow_agent_session_reset`` is enabled in config.yaml. It is OFF by
default so the agent cannot wipe a conversation unless the operator opts in.
"""

import json
from typing import Optional

from tools.registry import registry


def _active_profile() -> Optional[str]:
    """The profile serving the current session, if multiplexed.

    Under multiplex the gateway binds the active profile to the
    HERMES_SESSION_PROFILE contextvar for the duration of a message task, so
    we can scope the gate to that profile instead of the process-global base
    config that ``load_config()`` returns.

    Some plugin platforms (e.g. Keybase) bind to a profile without stamping
    ``source.profile``, so HERMES_SESSION_PROFILE may be empty even though the
    session is multiplexed. Fall back to the session key, which always carries
    the profile: ``agent:<profile>:...``. This keeps the gate correct even when
    the platform adapter doesn't populate the profile on its SessionSource.
    """
    try:
        from gateway.session_context import get_session_env

        profile = get_session_env("HERMES_SESSION_PROFILE", "") or ""
        if profile:
            return profile
        session_key = get_session_env("HERMES_SESSION_KEY", "") or ""
        parts = session_key.split(":")
        if len(parts) >= 2 and parts[0] == "agent":
            return parts[1] or None
    except Exception:
        return None
    return None


def _load_config_for_profile(profile: Optional[str]):
    """Load the config for ``profile`` (or the base config when None/'default').

    Named profiles are resolved via ``get_profile_dir(name)``, which is
    anchored to the hermes *root* (not the current ``HERMES_HOME``). That
    covers both multiplex layouts without double-nesting:

    - parent / pre-scope: ``HERMES_HOME=/opt/data`` + session profile ``kosima``
      → read ``/opt/data/profiles/kosima/config.yaml``
    - per-task override: ``HERMES_HOME`` already ``/opt/data/profiles/kosima``
      → still read that same profile path (never
      ``.../profiles/kosima/profiles/kosima/config.yaml``)

    Building the path from ``get_hermes_home() / "profiles" / name`` is wrong
    under the override; calling bare ``read_raw_config()`` is wrong when the
    override is *not* yet applied (it would read the base config and miss a
    profile-only flag).
    """
    try:
        if profile and profile != "default":
            from hermes_cli.profiles import get_profile_dir
            from hermes_cli.config import fast_safe_load

            cfg_path = get_profile_dir(profile) / "config.yaml"
            if not cfg_path.is_file():
                return None
            with open(cfg_path, encoding="utf-8") as f:
                data = fast_safe_load(f) or {}
            return data if isinstance(data, dict) else None
        from hermes_cli.config import load_config

        return load_config()
    except Exception:
        return None


def check_requirements() -> bool:
    """Only expose the tool when explicitly enabled for the active profile.

    Reads ``agent.allow_agent_session_reset``. The gate is profile-scoped: a
    flag set on a named profile (e.g. kosima) must not leak into the default
    profile, and the gateway process's base config must not hide a named
    profile's setting. So we load the *active* profile's config (via the
    session contextvar) rather than the process-global base config.
    """
    try:
        from hermes_cli.config import cfg_get

        cfg = _load_config_for_profile(_active_profile())
        return bool(cfg_get(cfg or {}, "agent", "allow_agent_session_reset") or False)
    except Exception:
        return False


async def reset_session_tool(args=None, **kwargs) -> str:
    """Reset the agent's current session (clear conversation history and start
    fresh) on behalf of the agent.

    Registry dispatch is ``handler(args, **kwargs)`` (kwargs include
    ``task_id``). Accept that signature and ignore both — the active session
    is resolved from gateway session context, not tool args.

    Returns a JSON string with ``success`` and the new ``session_id``, or an
    error describing why the reset could not be performed.
    """
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        if runner is None:
            return json.dumps(
                {"success": False, "error": "gateway runner unavailable"}
            )

        from gateway.session_context import get_session_env

        session_key = get_session_env("HERMES_SESSION_KEY")
        if not session_key:
            return json.dumps(
                {"success": False, "error": "no active session key in context"}
            )

        new_entry = await runner._reset_session_by_key(
            session_key, reason="agent_session_reset"
        )
        if new_entry is None:
            return json.dumps(
                {"success": False, "error": "reset produced no new session"}
            )
        return json.dumps(
            {"success": True, "new_session_id": new_entry.session_id}
        )
    except Exception as exc:  # surface, never raise into the tool caller
        return json.dumps({"success": False, "error": str(exc)})


registry.register(
    name="reset_session",
    # Named toolset must match TOOLSETS/CONFIGURABLE_TOOLSETS "session" so
    # platform reverse-mapping enables it. Still listed in _HERMES_CORE_TOOLS
    # so hermes-* composites include the tool name.
    toolset="session",
    schema={
        "name": "reset_session",
        "description": (
            "Reset the agent's current conversation session: clear all history "
            "and start a fresh session. Use ONLY when the user's intent is to "
            "discard the current conversation (for example they asked to start "
            "over or begin a new topic from scratch). This is equivalent to the "
            "user typing /reset and permanently discards the current "
            "conversation. Do not use it for ordinary topic switches where the "
            "history should be kept."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=reset_session_tool,
    check_fn=check_requirements,
    is_async=True,
    description="Reset the current session (agent-initiated /reset).",
    emoji="🔄",
)
