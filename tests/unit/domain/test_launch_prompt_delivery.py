"""A launch prompt reaches its agent through the run's prompt file (#9012)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.launch_prompt_delivery import (
    MAX_LAUNCH_COMMAND_BYTES,
    LaunchCommandTooLong,
    LaunchPromptFile,
    require_launch_command_within_bound,
)


def test_the_instruction_stays_short_however_long_the_prompt() -> None:
    huge = LaunchPromptFile.written(Path("/wt/run/session-prompt.txt"), "r" * 2_000_000)

    assert len(huge.instruction()) < 400


def test_a_prompt_file_must_be_an_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        LaunchPromptFile.written(Path("run/session-prompt.txt"), "prompt")


def test_a_command_within_the_bound_passes_through_unchanged() -> None:
    command = "c" * MAX_LAUNCH_COMMAND_BYTES

    assert require_launch_command_within_bound(command) is command


def test_a_command_over_the_bound_fails_loudly_rather_than_being_cut() -> None:
    # Measured in bytes, as the OS measures it: one multi-byte character over.
    command = "c" * (MAX_LAUNCH_COMMAND_BYTES - 1) + "é"

    with pytest.raises(LaunchCommandTooLong, match="argument bound"):
        require_launch_command_within_bound(command)
