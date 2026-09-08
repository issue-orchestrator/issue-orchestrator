"""Process-group adapter for deterministic tests that cannot spawn live agents."""

from pathlib import Path

from ..ports.command_runner import CommandRunner
from ..ports.contained_validation import ContainedValidationCommand


class LocalDeterministicValidationRunner:
    """Keep cheap model-free integration tests portable; never production-composed."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def reserved(self, evidence_directory: Path) -> bool:
        return False

    def run(self, command: ContainedValidationCommand):
        return self._runner.run(
            list(command.arguments), cwd=command.working_directory,
            env=command.environment, timeout_seconds=command.timeout_seconds,
        )
