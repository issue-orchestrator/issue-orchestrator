"""Port for reading run-scoped review artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ReviewArtifactReadCommand:
    """Command payload for reading one review artifact."""

    issue_number: int
    run_dir: Path
    artifact_path: str
    artifact_type: str


@dataclass(frozen=True)
class ReviewArtifactContent:
    """Human/UI-facing content returned for a review artifact command."""

    issue_number: int
    run_dir: Path
    artifact_path: Path
    artifact_type: str
    content_type: str
    content: str


class ReviewArtifactReader(Protocol):
    """Read review artifacts through the run-scoped artifact policy."""

    def read_review_artifact(
        self,
        command: ReviewArtifactReadCommand,
    ) -> ReviewArtifactContent:
        """Return content for the requested review artifact."""
        ...
