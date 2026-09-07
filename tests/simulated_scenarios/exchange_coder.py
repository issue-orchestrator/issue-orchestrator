"""Execute scripted coder attempts with the real allocated intake lifetime."""

from contextlib import contextmanager
import os
import shlex
import sys

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.ports.completion_intake import CompletionIntakeRuntime
from issue_orchestrator.ports.command_runner import CommandRunner


@contextmanager
def scripted_coder_attempts(
    owner: CompletionIntakeRuntime,
    run: SessionRunAssets,
    capability: str,
    agent: AgentConfig,
    runtime_config: RuntimeConfigReference,
    port: int | None,
    commands: CommandRunner,
):
    if owner.submission_capability(run) != capability:
        raise AssertionError("exchange capability does not bind allocated run")
    if port is None:
        raise AssertionError("scripted exchange requires its bound listener")
    intake = owner.bind_exchange(run)
    prompt = run.run_dir / "scripted-coder-prompt.md"
    prompt.write_text("Apply the scripted review correction.\n")
    environment = {
        **os.environ,
        **runtime_config.to_env(),
        "PATH": f"{sys.executable.rsplit('/', 1)[0]}:{os.environ.get('PATH', '')}",
        "ISSUE_ORCHESTRATOR_API_PORT": str(port),
        "ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY": capability,
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH": str(run.completion_record_copy.path),
        "ISSUE_ORCHESTRATOR_RUN_DIR": str(run.run_dir),
        "ISSUE_ORCHESTRATOR_SESSION_ID": run.session_name,
    }

    def attempt(number: int) -> None:
        intake.begin_attempt()
        assert agent.command is not None
        result = commands.run(
            ["bash", "-c", agent.command.format(prompt=shlex.quote(str(prompt)))],
            cwd=run.worktree_path,
            env={**environment, "SCENARIO_COMPLETION_ATTEMPT": str(number)},
            timeout_seconds=30,
        )
        if result.returncode != 0 or result.timed_out:
            raise AssertionError("scripted coder did not finish its submission")
        intake.completion_record()

    try:
        yield attempt
    finally:
        intake.close_and_drain()
