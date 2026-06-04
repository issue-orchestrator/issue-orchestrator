"""Typed lookup owner for previously recorded session runs."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..domain.session_run import SessionRunAssets
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecordedDebugRunResumeTarget:
    """Manifest-backed target for resuming a blocked debug run."""

    run_assets: SessionRunAssets
    completion_path: str

    def __post_init__(self) -> None:
        rel = Path(self.completion_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("completion_path must be relative and contained")

    @property
    def run_dir(self) -> Path:
        return self.run_assets.run_dir

    def completion_file(self) -> Path:
        return self.run_assets.worktree_path / self.completion_path


@dataclass(frozen=True, slots=True)
class RecordedSessionRunLookup:
    """Owner API for exact recorded session-run asset lookup."""

    session_output: SessionOutput

    def assets_for_exact_session(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> SessionRunAssets | None:
        run_dir = self.session_output.find_run_dir(
            worktree_path,
            session_name=session_name,
        )
        if run_dir is None:
            return None
        manifest = self.session_output.read_manifest(run_dir)
        if not isinstance(manifest, dict):
            return None
        try:
            return SessionRunAssets.from_manifest_payload(
                run_dir=run_dir,
                manifest=manifest,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Recorded session %s has invalid run assets at %s: %s",
                session_name,
                run_dir,
                exc,
            )
            return None

    def debug_resume_target(
        self,
        worktree_path: Path,
        issue_number: int,
    ) -> RecordedDebugRunResumeTarget | None:
        session_name = f"debug-{issue_number}"
        assets = self.assets_for_exact_session(
            worktree_path.resolve(),
            session_name,
        )
        if assets is None:
            return None
        if assets.session_name != session_name:
            return None
        if assets.worktree_path.resolve() != worktree_path.resolve():
            return None

        manifest = self.session_output.read_manifest(assets.run_dir)
        if not isinstance(manifest, dict):
            return None
        completion_path = _manifest_completion_path(manifest)
        if completion_path is None:
            return None
        try:
            return RecordedDebugRunResumeTarget(
                run_assets=assets,
                completion_path=completion_path,
            )
        except ValueError:
            return None


def _manifest_completion_path(manifest: Mapping[str, object]) -> str | None:
    raw_completion_path = manifest.get("completion_path")
    if not isinstance(raw_completion_path, str) or not raw_completion_path.strip():
        return None
    completion_path = raw_completion_path.strip()
    rel = Path(completion_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return completion_path
