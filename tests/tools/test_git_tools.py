"""Tests for profile-scoped agent Git tools."""

import json
from pathlib import Path

import pytest

from tools import git_tools


def test_git_tools_are_disabled_without_profile_opt_in(monkeypatch):
    monkeypatch.setattr(git_tools, "_load_active_profile_config", lambda: {})

    assert git_tools.check_requirements() is False


def test_repository_path_must_be_inside_configured_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        git_tools,
        "_load_active_profile_config",
        lambda: {"git": {"enabled": True, "workspace_root": str(workspace)}},
    )

    with pytest.raises(git_tools.GitToolError, match="outside the configured workspace"):
        git_tools._resolve_repository(str(outside))


def test_git_status_returns_structured_status(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        git_tools,
        "_load_active_profile_config",
        lambda: {"git": {"enabled": True, "workspace_root": str(tmp_path)}},
    )
    monkeypatch.setattr(
        git_tools,
        "_run_git",
        lambda cwd, args: (0, "## main\n M file.py\n", ""),
    )

    result = json.loads(git_tools.git_status_tool({"repository": str(repo)}))

    assert result == {
        "success": True,
        "repository": str(repo.resolve()),
        "status": "## main\n M file.py",
    }


def test_git_commit_is_profile_gated(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        git_tools,
        "_load_active_profile_config",
        lambda: {
            "git": {
                "enabled": True,
                "workspace_root": str(tmp_path),
                "allow_commit": False,
            }
        },
    )

    result = json.loads(
        git_tools.git_commit_tool(
            {"repository": str(repo), "message": "test commit"}
        )
    )

    assert result["success"] is False
    assert "allow_commit" in result["error"]
