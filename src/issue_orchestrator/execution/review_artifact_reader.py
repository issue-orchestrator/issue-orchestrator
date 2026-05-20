"""Manifest-backed review artifact reader."""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.review_artifact_reader import (
    ReviewArtifactContent,
    ReviewArtifactReadCommand,
)
from .manifest_accessor import ArtifactNotFoundError, ManifestAccessor, RunIdentity


@dataclass(frozen=True)
class ManifestReviewArtifactReader:
    """Read review artifacts through ``ManifestAccessor`` policy."""

    def read_review_artifact(
        self,
        command: ReviewArtifactReadCommand,
    ) -> ReviewArtifactContent:
        """Return content for one run-scoped review artifact command."""
        run_identity = RunIdentity(
            issue_number=command.issue_number,
            run_dir=command.run_dir,
        )
        artifact = ManifestAccessor(run_identity).get_review_artifact(
            artifact_path=command.artifact_path,
            artifact_type=command.artifact_type,
        )
        try:
            content = artifact.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ArtifactNotFoundError(
                f"failed to read review artifact: {artifact.path}"
            ) from exc
        return ReviewArtifactContent(
            issue_number=command.issue_number,
            run_dir=command.run_dir,
            artifact_path=artifact.path,
            artifact_type=artifact.descriptor.artifact_type,
            content_type=artifact.descriptor.content_type,
            content=content,
        )
