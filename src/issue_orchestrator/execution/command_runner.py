"""Local command runner adapter."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..infra.shutdown_signals import child_signal_reset_preexec
from ..ports.command_runner import CommandResult

logger = logging.getLogger(__name__)


class LocalCommandRunner:
    """Executes commands locally using subprocess."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
    ) -> CommandResult:
        logger.debug("Running command: %s", command)
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
                shell=shell,
                # Children must not inherit the orchestrator's blocked
                # SIGTERM/SIGINT mask (None where signals were never blocked, so
                # this is a no-op on macOS/Windows). See infra.shutdown_signals.
                preexec_fn=child_signal_reset_preexec(),
            )
            return CommandResult(
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode() if exc.stdout else "")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode() if exc.stderr else "")
            return CommandResult(
                returncode=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        except Exception as exc:
            logger.exception("Command execution failed")
            return CommandResult(
                returncode=-1,
                stdout="",
                stderr=str(exc),
                timed_out=False,
            )
