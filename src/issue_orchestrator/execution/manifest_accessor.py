"""Semantic run artifact access for a specific run identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from ..domain.run_manifest import RunManifest
from .session_output_adapter import FileSystemSessionOutput


@dataclass(frozen=True)
class RunIdentity:
    """Stable identity for a single run instance."""

    issue_number: int
    run_dir: Path
    run_id: str | None = None


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Descriptor for an artifact stream."""

    artifact_type: str
    run_identity: RunIdentity
    content_type: str
    encoding: str
    source_backend: str
    source_ref: str
    length_bytes: int | None
    updated_at: str | None


@dataclass(frozen=True)
class ArtifactStream:
    """Resolved artifact stream + descriptor."""

    descriptor: ArtifactDescriptor
    path: Path


class ArtifactNotFoundError(FileNotFoundError):
    """Raised when a run-scoped artifact cannot be resolved."""


def _worktree_path_from_run_dir(run_dir: Path) -> Path | None:
    """Infer worktree root from a run directory path."""
    parts = run_dir.resolve().parts
    if ".issue-orchestrator" not in parts:
        return None
    idx = parts.index(".issue-orchestrator")
    if idx <= 0:
        return None
    return Path(*parts[:idx])


@dataclass(frozen=True)
class ManifestAccessor:
    """Semantic accessor for run artifacts."""

    run_identity: RunIdentity

    def get_agent_log(self) -> ArtifactStream:
        """Return the run-scoped agent session log stream."""
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        candidates = self._agent_log_candidates(run_dir)
        existing = [candidate for candidate in candidates if candidate.exists()]
        non_empty = self._non_empty_paths(existing)
        if non_empty:
            return self._artifact_stream("agent_log", non_empty[0])
        if existing:
            candidates_str = ", ".join(str(path) for path in existing)
            raise ArtifactNotFoundError(
                f"agent_log candidates are empty under run_dir={run_dir}: {candidates_str}"
            )
        raise ArtifactNotFoundError(
            f"agent_log not found in run-scoped paths under: {run_dir}"
        )

    def _require_run_dir_exists(self, run_dir: Path) -> None:
        if not run_dir.exists():
            raise ArtifactNotFoundError(f"run_dir does not exist: {run_dir}")

    def _agent_log_candidates(self, run_dir: Path) -> list[Path]:
        worktree_path = _worktree_path_from_run_dir(run_dir)
        if not worktree_path:
            raise ArtifactNotFoundError(f"failed to infer worktree from run_dir: {run_dir}")
        session_output = FileSystemSessionOutput()
        session_name = session_output.session_name_from_path(str(run_dir))
        if not session_name:
            raise ArtifactNotFoundError(f"failed to infer session name from run_dir: {run_dir}")
        candidates: list[Path] = []
        session_candidate = session_output.get_log_path(worktree_path, session_name)
        if session_candidate:
            candidates.append(session_candidate)
        for candidate_name in ("session.log", "pane.log", "provider-runner/stdout.log"):
            candidate_path = run_dir / candidate_name
            if candidate_path not in candidates:
                candidates.append(candidate_path)
        return candidates

    def _non_empty_paths(self, candidates: list[Path]) -> list[Path]:
        non_empty: list[Path] = []
        for candidate in candidates:
            try:
                if candidate.stat().st_size > 0:
                    non_empty.append(candidate)
            except OSError:
                continue
        return non_empty

    def get_claude_log(self) -> ArtifactStream:
        """Return the run-scoped Claude transcript stream."""
        manifest = self._load_manifest()
        claude_path = manifest.claude_log_path
        if not claude_path:
            raise ArtifactNotFoundError("manifest missing claude_log_path")
        path = Path(claude_path)
        if not path.is_absolute():
            path = self.run_identity.run_dir / path
        if not path.exists() or path.stat().st_size <= 0:
            raise ArtifactNotFoundError(f"claude log not found: {path}")
        return self._artifact_stream("claude_log", path)

    def get_completion_record(self) -> ArtifactStream:
        """Return the completion record stream for this run."""
        manifest = self._load_manifest()
        completion_path = manifest.completion_path
        if not completion_path:
            raise ArtifactNotFoundError("manifest missing completion_path")
        path = Path(completion_path)
        if not path.is_absolute():
            worktree = _worktree_path_from_run_dir(self.run_identity.run_dir)
            if not worktree:
                raise ArtifactNotFoundError("failed to infer worktree for completion path")
            path = worktree / path
        if not path.exists():
            raise ArtifactNotFoundError(f"completion record not found: {path}")
        self._require_non_empty(path, artifact_name="completion record")
        self._require_valid_json(path, artifact_name="completion record")
        return self._artifact_stream(
            "completion_record",
            path,
            content_type="application/json",
        )

    def get_validation_record(self) -> ArtifactStream:
        """Return the validation record stream for this run."""
        manifest = self._load_manifest()
        validation_path = manifest.validation_record_path
        if not validation_path:
            raise ArtifactNotFoundError("manifest missing validation_record_path")
        path = Path(validation_path)
        if not path.is_absolute():
            path = self.run_identity.run_dir / path
        if not path.exists():
            raise ArtifactNotFoundError(f"validation record not found: {path}")
        self._require_non_empty(path, artifact_name="validation record")
        self._require_valid_json(path, artifact_name="validation record")
        return self._artifact_stream(
            "validation_record",
            path,
            content_type="application/json",
        )

    def _load_manifest(self) -> RunManifest:
        return RunManifest.load(self.run_identity.run_dir)

    def _artifact_stream(
        self,
        artifact_type: str,
        path: Path,
        *,
        content_type: str = "text/plain",
        encoding: str = "utf-8",
    ) -> ArtifactStream:
        stat = path.stat()
        descriptor = ArtifactDescriptor(
            artifact_type=artifact_type,
            run_identity=self.run_identity,
            content_type=content_type,
            encoding=encoding,
            source_backend="fs",
            source_ref=str(path),
            length_bytes=stat.st_size,
            updated_at=datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        )
        return ArtifactStream(descriptor=descriptor, path=path)

    def _require_non_empty(self, path: Path, *, artifact_name: str) -> None:
        size = path.stat().st_size
        if size <= 0:
            raise ArtifactNotFoundError(f"{artifact_name} is empty: {path}")

    def _require_valid_json(self, path: Path, *, artifact_name: str) -> None:
        try:
            text = path.read_text(encoding="utf-8")
            json.loads(text)
        except UnicodeDecodeError as exc:
            raise ArtifactNotFoundError(f"{artifact_name} is not utf-8: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ArtifactNotFoundError(f"{artifact_name} is invalid JSON: {path}") from exc
