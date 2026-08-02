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


def check_requirements() -> bool:
    """Only expose the tool when explicitly enabled in config.

    Reads ``agent.allow_agent_session_reset`` (default False). Kept as a config
    gate rather than an env var: it is non-secret behavior, and AGENTS.md
    requires behavioral settings to live in config.yaml, not .env.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config() or {}
        return bool(cfg_get(cfg, "agent", "allow_agent_session_reset") or False)
    except Exception:
        return False


async def reset_session_tool(task_id: str = None) -> str:
    """Reset the agent's current session (clear conversation history and start
    fresh) on behalf of the agent.

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
