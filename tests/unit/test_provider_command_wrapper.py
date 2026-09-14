"""Tests for provider command wrapping policy."""

import shlex
from pathlib import Path

from issue_orchestrator.control.provider_command_wrapper import ProviderCommandWrapper
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.provider_lane import BillingMode, ProviderLane
from issue_orchestrator.infra.config_models import ProviderShortRetryConfig


def _wrapper(*, lane: ProviderLane | None = None) -> ProviderCommandWrapper:
    return ProviderCommandWrapper(
        ProviderShortRetryConfig(
            max_attempts=2,
            initial_backoff_seconds=1,
            max_backoff_seconds=3,
            jitter=False,
        ),
        lane_for_agent=(lambda agent: lane) if lane is not None else None,
    )


def test_codex_default_interactive_skips_provider_runner(tmp_path: Path) -> None:
    agent = AgentConfig(prompt_path=tmp_path / "prompt.md", provider="codex")

    wrapped = _wrapper().wrap("codex 'task'", agent, tmp_path)

    assert wrapped == "codex 'task'"


def test_codex_exec_mode_uses_provider_runner(tmp_path: Path) -> None:
    agent = AgentConfig(
        prompt_path=tmp_path / "prompt.md",
        provider="codex",
        provider_args={"execution_mode": "exec"},
    )

    wrapped = _wrapper().wrap("codex exec 'task'", agent, tmp_path)

    argv = shlex.split(wrapped)
    assert argv[0]
    assert "issue_orchestrator.entrypoints.cli_tools.provider_runner" in argv
    assert "--provider" in argv
    assert argv[argv.index("--provider") + 1] == "codex"
    assert "--no-jitter" in argv


def test_codex_extra_exec_mode_uses_provider_runner(tmp_path: Path) -> None:
    agent = AgentConfig(prompt_path=tmp_path / "prompt.md", provider="codex")

    wrapped = _wrapper().wrap(
        "codex exec 'task'",
        agent,
        tmp_path,
        extra_provider_args={"execution_mode": "exec"},
    )

    assert "issue_orchestrator.entrypoints.cli_tools.provider_runner" in shlex.split(wrapped)


def test_providerless_command_does_not_ask_for_a_lane(tmp_path: Path) -> None:
    agent = AgentConfig(prompt_path=tmp_path / "prompt.md")

    def fail_if_called(_agent: AgentConfig) -> ProviderLane:
        raise AssertionError("providerless command has no configured lane")

    wrapper = ProviderCommandWrapper(
        _wrapper().retry_config,
        lane_for_agent=fail_if_called,
    )

    wrapped = wrapper.wrap("claude -p 'task'", agent, tmp_path)

    assert "issue_orchestrator.entrypoints.cli_tools.provider_runner" in wrapped


def test_one_shot_wrapper_reports_the_observed_lane_and_recovery_mode(
    tmp_path: Path,
) -> None:
    agent = AgentConfig(
        prompt_path=tmp_path / "prompt.md",
        provider="codex",
        provider_args={"execution_mode": "exec"},
    )

    wrapped = _wrapper(
        lane=ProviderLane("codex", "spark", BillingMode.PREPAID)
    ).wrap(
        "codex exec 'task'",
        agent,
        tmp_path,
    )

    argv = shlex.split(wrapped)
    assert argv[argv.index("--provider") + 1] == "codex:spark"
    assert "--quota-heals-on-timer" in argv
