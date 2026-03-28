"""Regression tests for _copy_hook_dir and _synthesize_gate_settings.

Ensures the AI gate test repo gets a minimal settings.json that preserves
tool-use hook registrations (PreToolUse, BeforeTool) while stripping
lifecycle hooks (Stop) and permissions that interfere with --print mode.
"""

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.hooks.hooks import (
    _copy_hook_dir,
    _synthesize_gate_settings,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a project with .claude hook dir and settings."""
    claude_dir = tmp_path / "project" / ".claude" / "hooks"
    claude_dir.mkdir(parents=True)
    (claude_dir / "block-no-verify.sh").write_text("#!/bin/bash\nexit 2\n")
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": ".claude/hooks/block-no-verify.sh",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo warning",
                            "timeout": 5,
                        }
                    ]
                }
            ],
        }
    }
    (tmp_path / "project" / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2)
    )
    (tmp_path / "project" / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git push:*)"]}})
    )
    return tmp_path / "project"


@pytest.fixture
def work_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "work"
    repo.mkdir()
    return repo


class TestCopyHookDir:
    def test_copies_hook_scripts(self, project: Path, work_repo: Path) -> None:
        _copy_hook_dir(project, work_repo, ".claude")
        assert (work_repo / ".claude" / "hooks" / "block-no-verify.sh").exists()

    def test_synthesizes_settings_with_pre_tool_use(
        self, project: Path, work_repo: Path
    ) -> None:
        _copy_hook_dir(project, work_repo, ".claude")
        settings_path = work_repo / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "PreToolUse" in settings["hooks"]

    def test_excludes_stop_hooks(
        self, project: Path, work_repo: Path
    ) -> None:
        _copy_hook_dir(project, work_repo, ".claude")
        settings = json.loads(
            (work_repo / ".claude" / "settings.json").read_text()
        )
        assert "Stop" not in settings["hooks"]

    def test_excludes_settings_local(
        self, project: Path, work_repo: Path
    ) -> None:
        _copy_hook_dir(project, work_repo, ".claude")
        assert not (work_repo / ".claude" / "settings.local.json").exists()

    def test_excludes_permissions(
        self, project: Path, work_repo: Path
    ) -> None:
        _copy_hook_dir(project, work_repo, ".claude")
        settings = json.loads(
            (work_repo / ".claude" / "settings.json").read_text()
        )
        assert "permissions" not in settings

    def test_missing_hook_dir_raises(
        self, project: Path, work_repo: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            _copy_hook_dir(project, work_repo, ".nonexistent")


class TestSynthesizeGateSettings:
    def test_preserves_before_tool_for_gemini(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        settings = {
            "hooks": {
                "BeforeTool": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": ".gemini/hooks/block-no-verify.sh",
                            }
                        ],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}],
            }
        }
        (src / "settings.json").write_text(json.dumps(settings))
        _synthesize_gate_settings(src, dst)
        result = json.loads((dst / "settings.json").read_text())
        assert "BeforeTool" in result["hooks"]
        assert "Stop" not in result["hooks"]

    def test_no_settings_file_is_noop(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _synthesize_gate_settings(src, dst)
        assert not (dst / "settings.json").exists()

    def test_no_relevant_hooks_skips_write(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        settings = {"hooks": {"Stop": [{"hooks": []}]}}
        (src / "settings.json").write_text(json.dumps(settings))
        _synthesize_gate_settings(src, dst)
        assert not (dst / "settings.json").exists()

    def test_malformed_json_is_noop(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "settings.json").write_text("not json{{{")
        _synthesize_gate_settings(src, dst)
        assert not (dst / "settings.json").exists()
