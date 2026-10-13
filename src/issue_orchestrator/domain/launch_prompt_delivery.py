"""How a launch prompt reaches the agent: through its run's prompt file (#9012).

A launch prompt is engine-composed and unbounded — an issue's standing rulings
(#8141), a health review's triage agenda (#7593), the rulings of every issue a
tech-lead run covers (#8347) — and a ruling may hold 30,000 characters. Embedded
in the launch command it could exceed the OS argument limit and the session
would fail to start. So no launch carries its prompt on the command line: the
prompt is persisted whole to the run directory and the command carries only a
short instruction to read that file (:meth:`LaunchPromptFile.instruction`).

What does still ride on the command line — provider flags, the completion
instructions, a sandbox's settings — is bounded by configuration, and
:func:`require_launch_command_within_bound` makes a command over the limit fail
loudly rather than reach the OS or be truncated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_guards import require_absolute_path

#: The whole launch command travels as ONE argument (``bash -lc <command>``), and
#: Linux caps a single argument at MAX_ARG_STRLEN (32 pages, 128 KiB). macOS has
#: no per-argument cap but a 1 MiB ARG_MAX for argv plus environment, which the
#: command, its exec'd provider argv and the environment also fit under. The
#: bound sits below 128 KiB to leave room for the session's environment exports
#: and a provider-runner wrapper added around the provider command.
MAX_LAUNCH_COMMAND_BYTES = 96 * 1024

#: How a launch command's prompt argument opens: the file's path follows.
LAUNCH_PROMPT_INSTRUCTION_OPENING = "Your complete launch prompt is the file "


class LaunchCommandTooLong(ValueError):
    """A launch command would exceed the OS argument limit.

    Raised before the command is handed to a terminal: the launch fails loudly
    and nothing in it is truncated to fit.
    """


def require_launch_command_within_bound(command: str) -> str:
    """Return *command* unchanged, or raise :class:`LaunchCommandTooLong`."""
    size = len(command.encode("utf-8"))
    if size > MAX_LAUNCH_COMMAND_BYTES:
        raise LaunchCommandTooLong(
            f"launch command is {size} bytes, over the {MAX_LAUNCH_COMMAND_BYTES}-byte"
            " argument bound; the session cannot be started with it"
        )
    return command


@dataclass(frozen=True, slots=True)
class LaunchPromptFile:
    """A launch prompt persisted whole to its run directory.

    Built only by the owner that wrote the file, from the text it wrote, so the
    size the instruction quotes is the size of the file the agent reads.
    """

    path: Path
    characters: int
    lines: int

    def __post_init__(self) -> None:
        require_absolute_path(self.path, "LaunchPromptFile.path")
        if self.characters < 0 or self.lines < 0:
            raise ValueError("LaunchPromptFile size must not be negative")

    @classmethod
    def written(cls, path: Path, text: str) -> LaunchPromptFile:
        """The prompt file at *path*, which holds exactly *text*."""
        return cls(path=path, characters=len(text), lines=len(text.splitlines()))

    def instruction(self) -> str:
        """The bounded prompt a launch command carries in place of the prompt."""
        return (
            f"{LAUNCH_PROMPT_INSTRUCTION_OPENING}{self.path}"
            f" ({self.lines} lines, {self.characters} characters)."
            " Read the whole file now, before doing anything else - in several"
            " reads if it is long - and follow it as the instructions for this session."
        )
