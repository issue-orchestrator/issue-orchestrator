"""The improver's runs, kept as files beside the repository's Git data (#7490).

``<git common dir>/io-improver/runs/<run id>/`` holds one run: its record
(``run.json``), the inputs it staged, the agent's final message and the
findings. The common Git directory is shared by every worktree of the
repository, so the budgeted suite's temporary checkout, an operator's shell
and the next run all read the same history.
"""

from __future__ import annotations

from pathlib import Path

from ..contracts.improver_findings import FINDINGS_FILE, ImproverFindings
from ..contracts.improver_run import ImproverRunRecord, RunOutcome
from ..infra.atomic_io import atomic_write_bytes
from ..ports.command_runner import CommandRunner

RUN_RECORD = "run.json"
STORE_DIRNAME = "io-improver"


class FileImproverRunStore:
    def __init__(self, root: Path) -> None:
        self._runs = root / "runs"

    @classmethod
    def for_checkout(cls, checkout: Path, runner: CommandRunner) -> "FileImproverRunStore":
        """The store of the repository ``checkout`` belongs to (any worktree of it)."""
        result = runner.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=checkout,
            timeout_seconds=60,
        )
        if result.returncode:
            raise RuntimeError(f"{checkout} is not a Git checkout: {result.stderr.strip()}")
        return cls(Path(result.stdout.strip()) / STORE_DIRNAME)

    def new_run_dir(self, run_id: str) -> Path:
        path = self._runs / run_id
        path.mkdir(parents=True, exist_ok=False)
        return path

    def record(self, run: ImproverRunRecord) -> None:
        atomic_write_bytes(
            self._runs / run.run_id / RUN_RECORD,
            (run.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )

    def runs(self) -> tuple[ImproverRunRecord, ...]:
        if not self._runs.is_dir():
            return ()
        records = [
            ImproverRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self._runs.glob(f"*/{RUN_RECORD}")
        ]
        return tuple(sorted(records, key=lambda r: (r.started_at, r.run_id), reverse=True))

    def accepted_findings(self, run: ImproverRunRecord) -> ImproverFindings:
        if run.outcome is not RunOutcome.ACCEPTED:
            raise ValueError(f"run {run.run_id} was {run.outcome.value}; it owes no findings")
        return ImproverFindings.model_validate_json(
            (self._runs / run.run_id / FINDINGS_FILE).read_text(encoding="utf-8")
        )


__all__ = ["FileImproverRunStore", "RUN_RECORD", "STORE_DIRNAME"]
