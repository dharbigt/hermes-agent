"""Agent-facing Git operations with profile-scoped safety controls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from hermes_cli._subprocess_compat import noninteractive_git_env
from tools.registry import registry


class GitToolError(ValueError):
    """A Git request failed validation before invoking Git."""


_MAX_OUTPUT = 32 * 1024


def _active_profile() -> Optional[str]:
    try:
        from gateway.session_context import get_session_env

        profile = get_session_env("HERMES_SESSION_PROFILE", "") or ""
        if profile:
            return profile
        key = get_session_env("HERMES_SESSION_KEY", "") or ""
        parts = key.split(":")
        if len(parts) >= 2 and parts[0] == "agent":
            return parts[1] or None
    except Exception:
        pass
    return None


def _load_active_profile_config() -> dict[str, Any]:
    """Load the config belonging to the current multiplexed session."""
    profile = _active_profile()
    try:
        if profile and profile != "default":
            from hermes_cli.config import fast_safe_load
            from hermes_cli.profiles import get_profile_dir

            path = get_profile_dir(profile) / "config.yaml"
            if not path.is_file():
                return {}
            with path.open(encoding="utf-8") as fh:
                data = fast_safe_load(fh) or {}
            return data if isinstance(data, dict) else {}
        from hermes_cli.config import load_config

        data = load_config() or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _git_config() -> dict[str, Any]:
    config = _load_active_profile_config().get("git", {})
    return config if isinstance(config, dict) else {}


def check_requirements() -> bool:
    """Expose Git tools only when enabled for the active profile."""
    config = _git_config()
    workspace = config.get("workspace_root")
    return bool(config.get("enabled", False)) and isinstance(workspace, str) and bool(
        workspace.strip()
    ) and Path(workspace).expanduser().is_dir() and bool(shutil.which("git"))


def _resolve_repository(raw: str | None) -> Path:
    config = _git_config()
    root_raw = config.get("workspace_root")
    if not isinstance(root_raw, str) or not root_raw.strip():
        raise GitToolError("git.workspace_root is not configured for this profile")
    root = Path(root_raw).expanduser().resolve(strict=True)
    repo = Path(raw or root).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise GitToolError("repository must be a directory")
    if not repo.is_relative_to(root):
        raise GitToolError("repository is outside the configured workspace")

    allowed = config.get("allowed_repositories")
    if isinstance(allowed, list) and allowed:
        allowed_paths = [Path(p).expanduser().resolve() for p in allowed if isinstance(p, str)]
        if not any(repo == allowed_repo or repo.is_relative_to(allowed_repo) for allowed_repo in allowed_paths):
            raise GitToolError("repository is not in git.allowed_repositories")
    return repo


def _run_git(cwd: Path, args: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=noninteractive_git_env(),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return completed.returncode, completed.stdout[:_MAX_OUTPUT], completed.stderr[:_MAX_OUTPUT]


def _error(exc: Exception) -> str:
    return json.dumps({"success": False, "error": str(exc)})


def git_status_tool(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        repo = _resolve_repository((args or {}).get("repository"))
        code, stdout, stderr = _run_git(repo, ["status", "--short", "--branch"])
        if code:
            return _error(GitToolError(stderr.strip() or "git status failed"))
        return json.dumps({"success": True, "repository": str(repo), "status": stdout.rstrip()})
    except Exception as exc:
        return _error(exc)


def git_diff_tool(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        args = args or {}
        repo = _resolve_repository(args.get("repository"))
        command = ["diff"]
        if args.get("staged"):
            command.append("--cached")
        code, stdout, stderr = _run_git(repo, command)
        if code:
            return _error(GitToolError(stderr.strip() or "git diff failed"))
        return json.dumps({"success": True, "repository": str(repo), "diff": stdout})
    except Exception as exc:
        return _error(exc)


def git_commit_tool(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        config = _git_config()
        if not config.get("allow_commit", False):
            raise GitToolError("git.allow_commit is disabled for this profile")
        args = args or {}
        message = str(args.get("message", "")).strip()
        if not message:
            raise GitToolError("commit message is required")
        repo = _resolve_repository(args.get("repository"))
        code, stdout, stderr = _run_git(repo, ["commit", "-m", message])
        if code:
            return _error(GitToolError(stderr.strip() or "git commit failed"))
        code, sha, stderr = _run_git(repo, ["rev-parse", "HEAD"])
        if code:
            return _error(GitToolError(stderr.strip() or "commit succeeded but SHA lookup failed"))
        return json.dumps({"success": True, "repository": str(repo), "commit": sha.strip(), "output": stdout})
    except Exception as exc:
        return _error(exc)


def git_push_tool(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        config = _git_config()
        if not config.get("allow_push", False):
            raise GitToolError("git.allow_push is disabled for this profile")
        args = args or {}
        repo = _resolve_repository(args.get("repository"))
        remote = str(args.get("remote", "origin")).strip()
        branch = str(args.get("branch", "")).strip()
        if not remote or remote.startswith("-") or branch.startswith("-"):
            raise GitToolError("invalid push remote or branch")
        command = ["push", remote] + ([branch] if branch else [])
        code, stdout, stderr = _run_git(repo, command)
        if code:
            return _error(GitToolError(stderr.strip() or "git push failed"))
        return json.dumps({"success": True, "repository": str(repo), "output": stdout})
    except Exception as exc:
        return _error(exc)


def git_create_pr_tool(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        config = _git_config()
        if not config.get("allow_pr", False):
            raise GitToolError("git.allow_pr is disabled for this profile")
        args = args or {}
        repo = _resolve_repository(args.get("repository"))
        title = str(args.get("title", "")).strip()
        if not title:
            raise GitToolError("pull request title is required")
        command = ["gh", "pr", "create", "--title", title, "--body", str(args.get("body", ""))]
        base = str(args.get("base", "")).strip()
        if base:
            if base.startswith("-"):
                raise GitToolError("invalid pull request base")
            command.extend(["--base", base])
        try:
            completed = subprocess.run(
                command,
                cwd=str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                env=noninteractive_git_env(),
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _error(GitToolError(str(exc)))
        if completed.returncode:
            return _error(GitToolError(completed.stderr.strip() or "gh pr create failed"))
        return json.dumps({"success": True, "repository": str(repo), "url": completed.stdout.strip()})
    except Exception as exc:
        return _error(exc)


_STATUS_SCHEMA = {
    "name": "git_status",
    "description": "Show the current branch and changed files for a profile-approved repository.",
    "parameters": {"type": "object", "properties": {"repository": {"type": "string"}}, "required": []},
}
_DIFF_SCHEMA = {
    "name": "git_diff",
    "description": "Show the working-tree or staged diff for a profile-approved repository.",
    "parameters": {"type": "object", "properties": {"repository": {"type": "string"}, "staged": {"type": "boolean"}}, "required": []},
}
_COMMIT_SCHEMA = {
    "name": "git_commit",
    "description": "Commit staged changes. Disabled unless git.allow_commit is enabled for the active profile.",
    "parameters": {"type": "object", "properties": {"repository": {"type": "string"}, "message": {"type": "string"}}, "required": ["message"]},
}

registry.register(name="git_status", toolset="git", schema=_STATUS_SCHEMA, handler=git_status_tool, check_fn=check_requirements, description="Show repository status.", emoji="📊")
registry.register(name="git_diff", toolset="git", schema=_DIFF_SCHEMA, handler=git_diff_tool, check_fn=check_requirements, description="Show repository diff.", emoji="🧾")
registry.register(name="git_commit", toolset="git", schema=_COMMIT_SCHEMA, handler=git_commit_tool, check_fn=check_requirements, description="Commit staged changes.", emoji="✅")
registry.register(name="git_push", toolset="git", schema={"name": "git_push", "description": "Push to a remote. Disabled unless git.allow_push is enabled for the active profile.", "parameters": {"type": "object", "properties": {"repository": {"type": "string"}, "remote": {"type": "string"}, "branch": {"type": "string"}}, "required": []}}, handler=git_push_tool, check_fn=check_requirements, description="Push repository changes.", emoji="⬆️")
registry.register(name="git_create_pr", toolset="git", schema={"name": "git_create_pr", "description": "Create a GitHub pull request with gh. Disabled unless git.allow_pr is enabled for the active profile.", "parameters": {"type": "object", "properties": {"repository": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}, "base": {"type": "string"}}, "required": ["title"]}}, handler=git_create_pr_tool, check_fn=check_requirements, description="Create a pull request.", emoji="🔗")
