from pathlib import Path

import pytest
import yaml

from issue_orchestrator.infra.config import AgentConfig, Config
from tests.e2e.fixtures.orchestrator_process import OrchestratorProcess


def test_write_e2e_config_preserves_agent_prompt_contract(tmp_path: Path) -> None:
    config = Config(
        repo="BruceBGordon/issue-orchestrator",
        repo_root=tmp_path,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "repo-specific" / "prompts" / "simple-fix.md",
            provider="claude-code",
            model="opus",
            timeout_minutes=75,
            initial_prompt="Work on issue #{issue_number}: stay focused and use --follow-up-file /tmp/follow-up-issues.jsonl.",
            provider_args={"permission_mode": "bypassPermissions", "verbose": "true"},
            retry_prompt_template="repo-specific/prompts/retry.md",
        )
    }

    process = OrchestratorProcess(config, tmp_path)
    try:
        config_path = process._write_e2e_config()  # noqa: SLF001

        payload = yaml.safe_load(config_path.read_text())
        agent_payload = payload["agents"]["agent:backend"]

        assert agent_payload["provider"] == "claude-code"
        assert agent_payload["model"] == "opus"
        assert agent_payload["timeout_minutes"] == 75
        assert (
            agent_payload["initial_prompt"]
            == config.agents["agent:backend"].initial_prompt
        )
        assert agent_payload["provider_args"] == {
            "permission_mode": "bypassPermissions",
            "verbose": "true",
        }
        assert agent_payload["retry_prompt_template"] == "repo-specific/prompts/retry.md"
    finally:
        process._close_log_file()  # noqa: SLF001


def test_write_e2e_config_uses_unique_path_per_process(tmp_path: Path) -> None:
    config = Config(
        repo="BruceBGordon/issue-orchestrator",
        repo_root=tmp_path,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )
    config.claims.enabled = True
    config.claims.claimant_id = "orchestrator-a"

    first = OrchestratorProcess(config, tmp_path)
    second = OrchestratorProcess(config, tmp_path)

    try:
        first_path = first._write_e2e_config()  # noqa: SLF001
        second_path = second._write_e2e_config()  # noqa: SLF001

        assert first_path != second_path
        assert first_path.parent != second_path.parent
        assert first_path.parent.name.startswith("e2e-orchestrator-config-")
        assert second_path.parent.name.startswith("e2e-orchestrator-config-")
    finally:
        first._close_log_file()  # noqa: SLF001
        second._close_log_file()  # noqa: SLF001


def test_start_runs_source_from_separate_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo-root"
    source_root = tmp_path / "source-root"
    repo_root.mkdir()
    source_root.mkdir()
    executable = source_root / ".venv" / "bin" / "issue-orchestrator"
    executable.parent.mkdir(parents=True)
    executable.touch()
    config = Config(
        repo="BruceBGordon/issue-orchestrator",
        repo_root=repo_root,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )

    process = OrchestratorProcess(config, repo_root, source_root=source_root)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        stdout = None
        stderr = None

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProcess:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "tests.e2e.fixtures.orchestrator_process.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(OrchestratorProcess, "wait_until_ready", lambda self: None)
    monkeypatch.setattr(OrchestratorProcess, "_log_reader", lambda self: None)

    try:
        process.start(max_issues=1)
    finally:
        process._close_log_file()  # noqa: SLF001

    cmd = captured["cmd"]
    env = captured["env"]
    assert isinstance(cmd, list)
    assert isinstance(env, dict)

    assert cmd[0] == str(executable)
    assert captured["cwd"] == repo_root
    assert str(source_root / "src") == env["PYTHONPATH"].split(":", maxsplit=1)[0]
