"""Tests for review exchange probe pairing logic."""

from pathlib import Path

from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.review_exchange_probe import _exchange_pairs


def test_exchange_pairs_ignore_unreviewable_agents(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Prompt")

    config = Config()
    config.review_enabled = True
    config.review_exchange_mode = "via-mcp"
    config.code_review_agent = "agent:reviewer"
    config.triage_review_agent = "agent:triage"
    config.agents = {
        "agent:coder": AgentConfig(prompt_path=prompt, ai_system="claude-code"),
        "agent:reviewer": AgentConfig(prompt_path=prompt, ai_system="codex"),
        "agent:triage": AgentConfig(prompt_path=prompt, ai_system="codex"),
        "agent:skip": AgentConfig(prompt_path=prompt, skip_review=True),
    }

    pairs = _exchange_pairs(config)

    assert ("agent:coder", "agent:reviewer") in pairs
    assert all(pair[0] != "agent:skip" for pair in pairs)
    assert all(pair[0] != "agent:triage" for pair in pairs)  # triage reviewer is excluded
