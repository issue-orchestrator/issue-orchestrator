"""How a launch prompt reaches the agent: through its run's prompt file (#9012).

A launch prompt is engine-composed and unbounded — an issue's standing rulings
(#8141), a health review's triage agenda (#7593), the rulings of every issue a
tech-lead run covers (#8347) — and a ruling may hold 30,000 characters. Embedded
in the launch command it could exceed the OS argument limit and the session
would fail to start. So no launch carries its prompt on the command line: the
prompt is persisted whole to the run directory and the command carries only a
short instruction to read that file (:meth:`LaunchPromptFile.instruction`).

What does still ride on the command line — provider flags, the completion
instructions, a sandbox's settings, the session's environment exports — is
bounded by configuration. A session is started from a :class:`LaunchCommand`,
the one finished form of everything the launcher composes, which cannot be
built over :data:`MAX_LAUNCH_COMMAND_BYTES`: a command over the limit fails
loudly before the launch mutates anything, rather than reaching the OS or being
truncated. The terminal that execs the command holds every argument it passes
the OS to :data:`MAX_EXEC_ARGUMENT_BYTES` (:func:`require_exec_argument_within_limit`).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .path_guards import require_absolute_path

#: The OS limit on one exec argument: Linux's MAX_ARG_STRLEN (32 pages, 128 KiB)
#: counts the terminating NUL. macOS has no per-argument cap but a 1 MiB ARG_MAX
#: for argv plus environment, which one argument this size also fits under. The
#: whole launch command travels as ONE argument (``bash -lc <command>``).
MAX_EXEC_ARGUMENT_BYTES = 128 * 1024 - 1

#: The bound on a session's :class:`LaunchCommand`, measured as the quoted shell
#: argument it travels as. It sits below :data:`MAX_EXEC_ARGUMENT_BYTES` to leave
#: room for what the terminal itself puts around the command: the working
#: directory, ``PATH`` and the isolation prefix.
MAX_LAUNCH_COMMAND_BYTES = 96 * 1024

#: How a launch command's prompt argument opens: the file's path follows.
LAUNCH_PROMPT_INSTRUCTION_OPENING = "Your complete launch prompt is the file "


class LaunchCommandTooLong(ValueError):
    """A launch command would exceed the OS argument limit.

    Raised before the command is handed to the OS: the launch fails loudly and
    nothing in it is truncated to fit.
    """


def quoted_argument_bytes(text: str) -> int:
    """The bytes *text* takes as one quoted shell argument.

    A command reaches ``bash -lc`` as a single argument of a shell command line,
    so it is quoted on the way, and a single quote it holds costs five bytes
    there (``'"'"'``): a quote-heavy command is far longer than it reads.
    """
    return len(shlex.quote(text).encode("utf-8"))


def require_exec_argument_within_limit(argument: str) -> str:
    """Return *argument* unchanged, or raise :class:`LaunchCommandTooLong`.

    For the terminal that execs a command: it checks the argument exactly as it
    passes it to the OS, after everything it put around the launch command.
    """
    size = len(argument.encode("utf-8"))
    if size > MAX_EXEC_ARGUMENT_BYTES:
        raise LaunchCommandTooLong(
            f"exec argument is {size} bytes, over the OS's {MAX_EXEC_ARGUMENT_BYTES}-byte"
            " argument limit; the session cannot be started with it"
        )
    return argument


@dataclass(frozen=True, slots=True)
class LaunchCommand:
    """A session's finished launch command, proven within the argument bound.

    The only form a launcher spawns a session from: the session's environment
    exports, then the agent command — the provider's, already wrapped for
    retries when the invocation is one-shot. Nothing is added to it before the
    terminal, so the bound checked here is the bound of what the launch can
    see; a command over it cannot be built, so a launch fails before it mutates
    anything.
    """

    text: str

    def __post_init__(self) -> None:
        size = quoted_argument_bytes(self.text)
        if size > MAX_LAUNCH_COMMAND_BYTES:
            raise LaunchCommandTooLong(
                f"launch command is {size} bytes as a quoted shell argument, over the"
                f" {MAX_LAUNCH_COMMAND_BYTES}-byte argument bound; the session cannot be"
                " started with it"
            )

    @classmethod
    def composed(cls, env_exports: str, agent_command: str) -> LaunchCommand:
        """The command that exports *env_exports*, then runs *agent_command*."""
        return cls(f"{env_exports} && {agent_command}")


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
