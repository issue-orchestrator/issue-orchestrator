"""Run the improver on Codex, non-interactively and read-only (#7490).

``codex exec`` in the run directory with the ``read-only`` sandbox: the agent
can read the staged inputs and the engine source there, and can write
nothing, reach no network and push nothing. Its final message IS its output;
Codex writes it to a file (``--output-last-message``) and the orchestrator
takes it from there. ``--ephemeral`` keeps the session out of Codex's own
history, and the prompt goes as the last argument, as every Codex launch in
the orchestrator passes it.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..ports.command_runner import CommandRunner
from ..ports.improver import ImproverAgentResult

#: Where Codex leaves the agent's last message, inside the run directory.
FINAL_MESSAGE_FILE = "improver-final-message.txt"


class CodexImproverAgent:
    def __init__(self, *, runner: CommandRunner, model: str, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("the improver agent needs a positive timeout")
        self._runner = runner
        self._model = model
        self._timeout = timeout_seconds

    def argv(self, *, prompt: str, run_dir: Path) -> list[str]:
        # stdin is /dev/null: ``codex exec`` appends a PIPED stdin to the
        # prompt, and would wait on one inherited from a runner that never
        # closes it.
        return [
            "/bin/sh", "-c", 'exec "$@" </dev/null', "sh",
            "codex", "exec",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color", "never",
            "--cd", str(run_dir),
            "--model", self._model,
            "--output-last-message", str(run_dir / FINAL_MESSAGE_FILE),
            prompt,
        ]

    def run(self, *, prompt: str, run_dir: Path) -> ImproverAgentResult:
        result = self._runner.run(
            self.argv(prompt=prompt, run_dir=run_dir),
            cwd=run_dir,
            env={**os.environ, "ISSUE_ORCHESTRATOR_RUN_DIR": str(run_dir)},
            timeout_seconds=self._timeout,
        )
        (run_dir / "improver-agent.log").write_text(
            result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8"
        )
        if result.timed_out:
            return ImproverAgentResult(None, f"codex timed out after {self._timeout}s")
        if result.returncode:
            return ImproverAgentResult(None, f"codex exited {result.returncode}: {result.stderr.strip()[-500:]}")
        message = run_dir / FINAL_MESSAGE_FILE
        if not message.is_file():
            return ImproverAgentResult(None, "codex finished without a final message")
        return ImproverAgentResult(message.read_text(encoding="utf-8"), "codex finished")


__all__ = ["CodexImproverAgent", "FINAL_MESSAGE_FILE"]
