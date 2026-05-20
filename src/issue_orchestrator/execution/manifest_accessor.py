"""Semantic run artifact access for a specific run identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from ..domain.review_artifacts import (
    REVIEW_DECISION_ARTIFACT,
    REVIEW_DECISION_FILENAME,
    REVIEW_REPORT_ARTIFACT,
    REVIEW_REPORT_FILENAME,
)
from ..domain.run_manifest import RunManifest
from .session_output_adapter import CLAUDE_SESSION_LOG_NAME


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


def _review_artifact_filename(artifact_type: str) -> str:
    if artifact_type == REVIEW_REPORT_ARTIFACT:
        return REVIEW_REPORT_FILENAME
    if artifact_type == REVIEW_DECISION_ARTIFACT:
        return REVIEW_DECISION_FILENAME
    raise ArtifactNotFoundError(f"unsupported review artifact type: {artifact_type}")


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

    def get_terminal_recording(self, *, allow_empty: bool = False) -> ArtifactStream:
        """Return the canonical raw terminal recording for this run."""
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        path = run_dir / "terminal-recording.jsonl"
        if path.exists() and (allow_empty or path.stat().st_size > 0):
            return self._artifact_stream(
                "terminal_recording",
                path,
                content_type="application/x-ndjson",
            )
        if path.exists():
            raise ArtifactNotFoundError(f"terminal recording is empty: {path}")
        raise ArtifactNotFoundError(f"terminal recording not found in run-scoped path: {path}")

    def get_review_exchange_phase_terminal_recording(
        self,
        *,
        round_index: int,
        role: str,
        allow_empty: bool = False,
    ) -> ArtifactStream:
        """Return the raw terminal recording for one review-exchange phase.

        Resolution order (newest layout first):

        1. **Persistent runner slice (B2 onward).** The per-exchange
           manifest carries ``coder_recording`` / ``reviewer_recording``
           keys pointing at ``<run_dir>/<role>/terminal-recording.jsonl``.
           The continuous pair-scoped recordings live under the coder
           worktree and are exposed separately via ``*_recording_pair``.
           Chapter offsets in ``chapters.json`` tell the replay UI how
           to scrub each per-exchange slice.
        2. **B1 / pre-B2 persistent layout.**
           ``<run_dir>/<role>/terminal-recording.jsonl`` —
           per-exchange recording, no manifest indirection.
        3. **Legacy spawn-per-phase layout.**
           ``<run_dir>/review-exchange/round-NNN/<role>/terminal-recording.jsonl``
           for runs from before the persistent-session cutover.
        """
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        normalized_role = str(role).strip().lower()
        if round_index <= 0:
            raise ArtifactNotFoundError(f"invalid review exchange round: {round_index}")
        if normalized_role not in {"reviewer", "coder"}:
            raise ArtifactNotFoundError(f"invalid review exchange role: {role}")

        manifest_path = self._read_manifest_recording_path(normalized_role)
        persistent_path = run_dir / normalized_role / "terminal-recording.jsonl"
        legacy_path = (
            run_dir
            / "review-exchange"
            / f"round-{round_index:03d}"
            / normalized_role
            / "terminal-recording.jsonl"
        )

        for candidate in (manifest_path, persistent_path, legacy_path):
            if candidate is None:
                continue
            if candidate.exists() and (
                allow_empty or candidate.stat().st_size > 0
            ):
                return self._artifact_stream(
                    "terminal_recording",
                    candidate,
                    content_type="application/x-ndjson",
                )

        # All candidates either missing or empty; preserve the
        # informative-empty diagnostic the previous code emitted.
        for candidate in (manifest_path, persistent_path, legacy_path):
            if candidate is not None and candidate.exists():
                raise ArtifactNotFoundError(
                    f"review exchange recording is empty: {candidate}"
                )
        raise ArtifactNotFoundError(
            f"review exchange recording not found for "
            f"round={round_index} role={normalized_role}; "
            f"checked manifest={manifest_path} "
            f"persistent={persistent_path} legacy={legacy_path}"
        )

    def _read_manifest_recording_path(self, role: str) -> Path | None:
        """Resolve the role's pair-scoped recording from the manifest.

        Returns ``None`` if the manifest is absent, is unreadable, or
        does not carry a ``<role>_recording`` key (i.e. pre-B2 runs).
        """
        manifest_file = self.run_identity.run_dir / "manifest.json"
        if not manifest_file.exists():
            return None
        try:
            payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        key = f"{role}_recording"
        value = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            return None
        return Path(value)

    def get_agent_log(self, *, allow_empty: bool = False) -> ArtifactStream:
        """Return the canonical run-scoped agent recording stream."""
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        terminal_path = run_dir / "terminal-recording.jsonl"
        if terminal_path.exists() and terminal_path.stat().st_size > 0:
            artifact = self._artifact_stream(
                "agent_log",
                terminal_path,
                content_type="application/x-ndjson",
            )
            return artifact
        if terminal_path.exists() and allow_empty:
            return self._artifact_stream(
                "agent_log",
                terminal_path,
                content_type="application/x-ndjson",
            )
        if terminal_path.exists():
            raise ArtifactNotFoundError(f"terminal recording is empty: {terminal_path}")
        raise ArtifactNotFoundError(f"agent log not found in run-scoped path: {run_dir}")

    def _require_run_dir_exists(self, run_dir: Path) -> None:
        if not run_dir.exists():
            raise ArtifactNotFoundError(f"run_dir does not exist: {run_dir}")

    def _claude_log_candidates(self, run_dir: Path, manifest: dict[str, Any]) -> list[Path]:
        """Return potential Claude log files for the run."""
        candidates: list[Path] = []
        log_path = manifest.get("claude_log_path")
        if log_path:
            candidate = Path(log_path)
            if not candidate.is_absolute():
                candidate = run_dir / log_path
            candidates.append(candidate)

        log_dir = manifest.get("claude_log_dir")
        if log_dir:
            candidate_dir = Path(log_dir)
            if not candidate_dir.is_absolute():
                candidate_dir = run_dir / log_dir
            if candidate_dir.exists():
                jsonl_files = sorted(
                    candidate_dir.glob("*.jsonl"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                candidates.extend(jsonl_files)

        claude_symlink = run_dir / CLAUDE_SESSION_LOG_NAME
        if claude_symlink.exists():
            candidates.append(claude_symlink)

        return candidates

    def get_claude_log(self) -> ArtifactStream:
        """Return the run-scoped Claude transcript stream."""
        manifest = self._load_manifest()
        candidates = self._claude_log_candidates(self.run_identity.run_dir, manifest.to_dict())
        if not candidates:
            raise ArtifactNotFoundError("manifest missing claude log candidates")
        for path in candidates:
            if path.exists() and path.stat().st_size > 0:
                return self._artifact_stream("claude_log", path)
        raise ArtifactNotFoundError(
            "claude log not found: "
            + ", ".join(str(path) for path in candidates)
        )

    def get_review_exchange_transcript(self, *, allow_empty: bool = False) -> ArtifactStream:
        """Return the dedicated review-exchange transcript for this run."""
        manifest = self._load_manifest()
        transcript_path = manifest.to_dict().get("review_exchange_transcript_path")
        if not transcript_path:
            raise ArtifactNotFoundError("manifest missing review_exchange_transcript_path")
        path = Path(str(transcript_path))
        if not path.is_absolute():
            path = self.run_identity.run_dir / path
        if not path.exists():
            raise ArtifactNotFoundError(f"review exchange transcript not found: {path}")
        if not allow_empty:
            self._require_non_empty(path, artifact_name="review exchange transcript")
        return self._artifact_stream("review_exchange_transcript", path)

    def get_review_artifact(
        self,
        *,
        artifact_path: str,
        artifact_type: str,
    ) -> ArtifactStream:
        """Return a review report/decision artifact scoped to this run."""
        run_dir = self.run_identity.run_dir.resolve()
        self._require_run_dir_exists(run_dir)
        candidate = Path(artifact_path)
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run_dir)
        except (OSError, ValueError) as exc:
            raise ArtifactNotFoundError(
                f"review artifact path escapes run_dir: {candidate}"
            ) from exc
        turns_dir = (run_dir / "review-exchange" / "turns").resolve()
        expected_filename = _review_artifact_filename(artifact_type)
        if resolved.parent != turns_dir or not resolved.name.endswith(f".{expected_filename}"):
            raise ArtifactNotFoundError(
                "review artifact path is not a persisted review turn artifact: "
                f"{resolved}"
            )
        if not resolved.exists() or not resolved.is_file():
            raise ArtifactNotFoundError(f"review artifact not found: {resolved}")
        self._require_non_empty(resolved, artifact_name="review artifact")
        if artifact_type == REVIEW_DECISION_ARTIFACT:
            self._require_valid_json(resolved, artifact_name="review decision")
            content_type = "application/json"
        elif artifact_type == REVIEW_REPORT_ARTIFACT:
            content_type = "text/markdown"
        else:
            raise ArtifactNotFoundError(f"unsupported review artifact type: {artifact_type}")
        return self._artifact_stream(
            artifact_type,
            resolved,
            content_type=content_type,
        )

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
