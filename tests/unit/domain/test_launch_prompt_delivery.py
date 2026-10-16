"""A launch prompt reaches its agent through the run's prompt file (#9012)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.launch_prompt_delivery import (
    MAX_EXEC_ARGUMENT_BYTES,
    LaunchCommand,
    LaunchCommandTooLong,
    LaunchPromptFile,
    PreparedSessionCommand,
    require_exec_argument_within_limit,
)


def test_the_instruction_stays_short_however_long_the_prompt() -> None:
    huge = LaunchPromptFile.written(Path("/wt/run/session-prompt.txt"), "r" * 2_000_000)

    assert len(huge.instruction()) < 400


def test_a_prompt_file_must_be_an_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        LaunchPromptFile.written(Path("run/session-prompt.txt"), "prompt")


def test_a_launch_command_exports_the_environment_then_runs_the_agent() -> None:
    command = LaunchCommand.composed("export A='1'", "claude 'Read the file'")

    assert command.text == "export A='1' && claude 'Read the file'"


def test_a_launch_command_carries_no_bound_of_its_own() -> None:
    """Only the terminal knows what it adds around a command, so no fixed
    reserve below the OS limit stands in for it."""
    text = "c" * (MAX_EXEC_ARGUMENT_BYTES * 2)

    assert LaunchCommand(text).text is text


def _prepared(*exec_argv: str) -> PreparedSessionCommand:
    return PreparedSessionCommand(
        launch=LaunchCommand("claude"), working_dir=Path("/wt"), argv=("claude",), exec_argv=exec_argv,
    )


def test_a_prepared_command_within_the_os_limit_is_kept_unchanged() -> None:
    argument = "a" * MAX_EXEC_ARGUMENT_BYTES

    assert _prepared("/bin/bash", "-c", argument).exec_argv[2] is argument


def test_a_prepared_command_with_any_argument_over_the_os_limit_cannot_be_built() -> None:
    # Measured in bytes, as the OS measures it: one multi-byte character over.
    with pytest.raises(LaunchCommandTooLong, match="OS's"):
        _prepared("/bin/bash", "-c", "a" * (MAX_EXEC_ARGUMENT_BYTES - 1) + "é")


def test_a_prepared_command_needs_what_it_runs_and_how_it_is_execd() -> None:
    with pytest.raises(ValueError, match="exec'd"):
        _prepared()


def test_an_exec_argument_within_the_os_limit_passes_through_unchanged() -> None:
    argument = "a" * MAX_EXEC_ARGUMENT_BYTES

    assert require_exec_argument_within_limit(argument) is argument


def test_an_exec_argument_over_the_os_limit_fails_loudly_rather_than_being_cut() -> None:
    with pytest.raises(LaunchCommandTooLong, match="OS's"):
        require_exec_argument_within_limit("a" * (MAX_EXEC_ARGUMENT_BYTES + 1))
