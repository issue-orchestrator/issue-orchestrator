"""Export the io source tree an engine runs, at its exact commit (#7490).

The improver reads the engine's source read-only, at the commit the engine
recorded when it started, never at whatever a checkout happens to hold now.
``git archive`` writes that commit's tree to a tar file, which is unpacked
with the ``data`` filter (no links out of the tree, no device files).
"""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path

from ..ports.command_runner import CommandRunner

#: Seconds ``git archive`` of one commit may take.
ARCHIVE_TIMEOUT_SECONDS = 300


class EngineSourceUnavailable(RuntimeError):
    """The engine's commit is not in the repository the source is taken from."""


class GitEngineSourceArchive:
    """Exports commits of one io repository."""

    def __init__(self, repo: Path, runner: CommandRunner) -> None:
        self._repo = repo
        self._runner = runner

    def export(self, commit: str, destination: Path) -> None:
        """Write ``commit``'s tree under ``destination``, which must not exist."""
        if destination.exists():
            raise FileExistsError(f"engine source destination already exists: {destination}")
        known = self._runner.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=self._repo,
            timeout_seconds=60,
        )
        if known.returncode:
            raise EngineSourceUnavailable(
                f"the engine's commit {commit} is not in {self._repo}; fetch it first"
            )
        with tempfile.TemporaryDirectory(prefix="io-engine-source-") as scratch:
            archive = Path(scratch) / "source.tar"
            result = self._runner.run(
                ["git", "archive", "--format=tar", f"--output={archive}", commit],
                cwd=self._repo,
                timeout_seconds=ARCHIVE_TIMEOUT_SECONDS,
            )
            if result.returncode or result.timed_out:
                raise EngineSourceUnavailable(
                    f"git archive of {commit} failed: {result.stderr.strip()}"
                )
            destination.mkdir(parents=True)
            with tarfile.open(archive) as tar:
                tar.extractall(destination, filter="data")


__all__ = ["ARCHIVE_TIMEOUT_SECONDS", "EngineSourceUnavailable", "GitEngineSourceArchive"]
