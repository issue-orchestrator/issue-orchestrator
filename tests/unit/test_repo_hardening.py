"""Tests for repo-level hardening installation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_hardening import (
    _render_repo_pre_push_hook,
    harden_repo,
    inspect_repo_hardening,
    RepoHardeningError,
)


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_config(repo: Path) -> Config:
    config = Config(repo_root=repo)
    config.validation.cmd = "make validate-pr"
    config.agents = {
        "agent:dev": AgentConfig(
            prompt_path=repo / "prompt.md",
            command="claude --print",
        )
    }
    return config


def test_harden_repo_installs_repo_guardrails_and_agent_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    config = _make_config(repo)
    result = harden_repo(config)

    hooks_path = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert hooks_path == ".githooks"
    assert result.pre_push_hook == repo / ".githooks" / "pre-push"
    assert result.pre_push_hook.exists()
    assert result.verify_script.exists()
    assert result.helper_script.exists()
    assert (repo / ".claude" / "hooks" / "block-no-verify.sh").exists()
    assert (repo / ".claude" / "settings.json").exists()

    hook_result = subprocess.run(
        [str(repo / ".claude" / "hooks" / "block-no-verify.sh")],
        cwd=repo,
        input=json.dumps(
            {"tool_input": {"command": "git config --local core.hooksPath /dev/null"}}
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert hook_result.returncode == 2
    assert "BLOCKED" in hook_result.stderr

    status = inspect_repo_hardening(repo, config=config)
    assert status.pre_push_managed
    assert status.pre_push_calls_verify
    assert status.verify_managed
    assert status.helper_executable
    assert status.helper_managed
    assert status.agent_hooks["claude-code"].installed
    assert all(
        managed.exists and managed.executable and managed.matches_template is True
        for managed in status.agent_hooks["claude-code"].managed_files
    )


def test_harden_repo_preserves_existing_pre_push_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", ".githooks"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    original_hook = hooks_dir / "pre-push"
    original_hook.write_text("#!/usr/bin/env bash\necho original-hook\n")
    original_hook.chmod(0o755)

    result = harden_repo(_make_config(repo))

    preserved = hooks_dir / "pre-push.project"
    assert preserved in result.preserved_files
    assert preserved.read_text() == "#!/usr/bin/env bash\necho original-hook\n"
    assert "scripts/verify-pr.sh" in result.pre_push_hook.read_text()


def test_harden_repo_recovers_from_external_existing_hooks_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", str(external_hooks)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    config = _make_config(repo)
    result = harden_repo(config)

    hooks_path = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert hooks_path == ".githooks"
    assert result.hooks_dir == repo / ".githooks"
    assert result.pre_push_hook.exists()
    assert result.verify_script.exists()
    assert result.helper_script.exists()


def test_harden_repo_rejects_explicit_external_hooks_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()

    with pytest.raises(
        RepoHardeningError,
        match="core.hooksPath must resolve inside the repository",
    ):
        harden_repo(_make_config(repo), hooks_path=str(external_hooks))


def test_render_repo_pre_push_hook_uses_repo_root_relative_path() -> None:
    repo_root = Path("/tmp/example-repo")
    verify_script = repo_root / "scripts" / "gates" / "verify-pr.sh"

    rendered = _render_repo_pre_push_hook(verify_script, repo_root)

    assert 'VERIFY_SCRIPT="$REPO_ROOT/scripts/gates/verify-pr.sh"' in rendered


def test_harden_repo_refreshes_drifted_agent_hook_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    config = _make_config(repo)
    harden_repo(config)

    hook_path = repo / ".claude" / "hooks" / "block-no-verify.sh"
    hook_path.write_text("#!/usr/bin/env bash\necho drifted\n")
    hook_path.chmod(0o755)

    status_before = inspect_repo_hardening(repo, config=config)
    assert (
        status_before.agent_hooks["claude-code"].managed_files[0].matches_template
        is False
    )

    harden_repo(config)

    status_after = inspect_repo_hardening(repo, config=config)
    assert (
        status_after.agent_hooks["claude-code"].managed_files[0].matches_template
        is True
    )
