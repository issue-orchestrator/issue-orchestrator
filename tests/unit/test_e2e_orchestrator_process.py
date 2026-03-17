from pathlib import Path

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
            permission_mode="bypassPermissions",
            initial_prompt="Work on issue #{issue_number}: stay focused and use --follow-up-file /tmp/follow-up-issues.jsonl.",
            provider_args={"permission_mode": "bypassPermissions", "verbose": "true"},
            retry_prompt_template="repo-specific/prompts/retry.md",
        )
    }

    process = OrchestratorProcess(config, tmp_path)
    config_path = process._write_e2e_config()

    payload = yaml.safe_load(config_path.read_text())
    agent_payload = payload["agents"]["agent:backend"]

    assert agent_payload["provider"] == "claude-code"
    assert agent_payload["model"] == "opus"
    assert agent_payload["timeout_minutes"] == 75
    assert agent_payload["initial_prompt"] == config.agents["agent:backend"].initial_prompt
    assert agent_payload["provider_args"] == {
        "permission_mode": "bypassPermissions",
        "verbose": "true",
    }
    assert agent_payload["retry_prompt_template"] == "repo-specific/prompts/retry.md"
