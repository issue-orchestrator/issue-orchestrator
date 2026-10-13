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
bounded by configuration, and the terminal adds its own part: the working
directory, the inherited ``PATH`` and its isolation prefix. Only the terminal
knows that part, so only the terminal can say whether a command fits. A launch
composes a :class:`LaunchCommand` and asks its terminal to prepare it; the
terminal answers with a :class:`PreparedSessionCommand` — the exact arguments it
will exec, each proven within :data:`MAX_EXEC_ARGUMENT_BYTES` — or raises
:class:`LaunchCommandTooLong`. A launch prepares before its first workflow
mutation, and the terminal starts the session from that same prepared value, so
a command the OS would refuse fails loudly before anything changed, and the
argument checked is the argument exec'd.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_guards import require_absolute_path

#: The OS limit on one exec argument: Linux's MAX_ARG_STRLEN (32 pages, 128 KiB)
#: counts the terminating NUL. macOS has no per-argument cap but a 1 MiB ARG_MAX
#: for argv plus environment, which one argument this size also fits under.
MAX_EXEC_ARGUMENT_BYTES = 128 * 1024 - 1

#: How a launch command's prompt argument opens: the file's path follows.
LAUNCH_PROMPT_INSTRUCTION_OPENING = "Your complete launch prompt is the file "


class LaunchCommandTooLong(ValueError):
    """A session's exec arguments would exceed the OS argument limit.

    Raised by the terminal that prepares the command, before the launch mutates
    anything: the launch fails loudly and nothing in it is truncated to fit.
    """


def require_exec_argument_within_limit(argument: str) -> str:
    """Return *argument* unchanged, or raise :class:`LaunchCommandTooLong`.

    For an argument exactly as it is passed to the OS.
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
    """A session's launch command as the launcher finishes it.

    The session's environment exports, then the agent command — the
    provider's, already wrapped for retries when the invocation is one-shot.
    It is not yet what the OS execs: the terminal puts its own part around it,
    so only a :class:`PreparedSessionCommand` says whether it can be started.
    """

    text: str

    @classmethod
    def composed(cls, env_exports: str, agent_command: str) -> LaunchCommand:
        """The command that exports *env_exports*, then runs *agent_command*."""
        return cls(f"{env_exports} && {agent_command}")


@dataclass(frozen=True, slots=True)
class PreparedSessionCommand:
    """A launch command exactly as its terminal will exec it, proven to fit.

    Built only by the terminal that will start the session, which alone knows
    what it puts around *launch* and how it hands the result to the OS.
    ``argv`` is what the terminal runs; ``exec_argv`` is how the OS receives
    it, a pure function of ``argv`` the terminal's runner applies again when it
    starts the session — so nothing the terminal reads at start (``PATH``, its
    isolation prefix) can differ from what was checked here.
    """

    launch: LaunchCommand
    working_dir: Path
    argv: tuple[str, ...]
    exec_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.argv or not self.exec_argv:
            raise ValueError("PreparedSessionCommand needs the command it runs and how it is exec'd")
        for argument in self.exec_argv:
            require_exec_argument_within_limit(argument)


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
