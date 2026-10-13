"""A launch prompt reaches its agent through the run's prompt file (#9012)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.launch_prompt_delivery import (
    MAX_EXEC_ARGUMENT_BYTES,
    MAX_LAUNCH_COMMAND_BYTES,
    LaunchCommand,
    LaunchCommandTooLong,
    LaunchPromptFile,
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


def test_a_command_within_the_bound_is_kept_unchanged() -> None:
    text = "c" * MAX_LAUNCH_COMMAND_BYTES

    assert LaunchCommand(text).text is text


def test_a_command_over_the_bound_cannot_be_built() -> None:
    # Measured in bytes, as the OS measures it: one multi-byte character over.
    with pytest.raises(LaunchCommandTooLong, match="argument bound"):
        LaunchCommand("c" * (MAX_LAUNCH_COMMAND_BYTES - 1) + "é")


def test_a_command_is_measured_as_the_quoted_argument_it_travels_as() -> None:
    """A single quote takes five bytes once the command is quoted into ``bash
    -lc``, so a quote-heavy command reading well under the bound is over it."""
    text = "'" * (MAX_LAUNCH_COMMAND_BYTES // 4)
    assert len(text.encode()) < MAX_LAUNCH_COMMAND_BYTES

    with pytest.raises(LaunchCommandTooLong, match="quoted shell argument"):
        LaunchCommand(text)


def test_the_bound_leaves_the_terminal_room_below_the_os_limit() -> None:
    assert MAX_EXEC_ARGUMENT_BYTES - MAX_LAUNCH_COMMAND_BYTES >= 16 * 1024


def test_an_exec_argument_within_the_os_limit_passes_through_unchanged() -> None:
    argument = "a" * MAX_EXEC_ARGUMENT_BYTES

    assert require_exec_argument_within_limit(argument) is argument


def test_an_exec_argument_over_the_os_limit_fails_loudly_rather_than_being_cut() -> None:
    with pytest.raises(LaunchCommandTooLong, match="OS's"):
        require_exec_argument_within_limit("a" * (MAX_EXEC_ARGUMENT_BYTES + 1))
