"""Launch an IO-owned worker that can finish across repository-engine restarts."""

import json
import logging
from pathlib import Path
import subprocess
import sys
import uuid

from ..domain.budgeted_validation import BudgetedValidationSuite
from ..infra.shutdown_signals import child_signal_reset_preexec

logger = logging.getLogger(__name__)


class BudgetedValidationWorkerProcess:
    def __init__(self, *, repo_root: Path, directory: Path, suites: tuple[BudgetedValidationSuite, ...]) -> None:
        self._repo_root = repo_root
        self._directory = directory
        self._suites = suites
        self._process: subprocess.Popen[bytes] | None = None

    def running(self) -> bool:
        if self._process is None:
            return False
        result = self._process.poll()
        if result is None:
            return True
        if result:
            logger.error("Budgeted validation worker exited %s; evidence: %s", result, self._directory)
        self._process = None
        return False

    def start(self) -> None:
        from dataclasses import asdict

        if self.running():
            raise RuntimeError("Budgeted validation worker is already running")
        self._directory.mkdir(parents=True, exist_ok=True)
        request = self._directory / f"request-{uuid.uuid4().hex}.json"
        request.write_text(json.dumps({
            "repo_root": str(self._repo_root),
            "suites": {suite.name: {key: value for key, value in asdict(suite).items() if key != "name"} for suite in self._suites},
        }))
        # The child holds the shared lease itself. Restarting the engine cannot
        # release it while tests continue, nor allow another worktree to race it.
        with (self._directory / "worker.log").open("ab") as log:
            self._process = subprocess.Popen([
                sys.executable, "-m", "issue_orchestrator.entrypoints.budgeted_validation_worker",
                "--request", str(request),
            ], cwd=self._repo_root, stdout=log, stderr=log, start_new_session=True,
                preexec_fn=child_signal_reset_preexec())
