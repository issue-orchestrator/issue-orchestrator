"""Triage manifest - defines what data to fetch for triage sessions.

The orchestrator creates a manifest listing PRs to review, then a downloader
fetches the data (diffs, metadata) and writes it locally. The triage agent
reads the manifest to find its work.

Flow:
1. Orchestrator: build_triage_manifest() -> TriageManifest
2. Downloader: download_manifest_data() -> writes files, updates manifest
3. Agent: reads manifest.json, reads local files, reports via agent-done
4. Orchestrator: adds triage-reviewed label to all PRs in manifest
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class PRFilesDict(TypedDict):
    """Serialized form of PRFiles."""

    diff: str
    metadata: str


class PRToReviewDict(TypedDict):
    """Serialized form of PRToReview."""

    number: int
    title: str
    url: str
    branch: str
    files: PRFilesDict


class TriageManifestDict(TypedDict):
    """Serialized form of TriageManifest."""

    session_type: str
    generated_at: str
    data_dir: str
    prs: list[PRToReviewDict]


@dataclass
class PRFiles:
    """Local file paths for a PR's data."""
    diff: str = ""  # Relative path to diff file
    metadata: str = ""  # Relative path to metadata JSON


@dataclass
class PRToReview:
    """A PR that needs triage review.

    Note: Full PR metadata (additions, deletions, merged_at, etc.) is available
    in the metadata JSON file referenced by files.metadata.
    """
    number: int
    title: str
    url: str
    branch: str
    files: PRFiles = field(default_factory=PRFiles)


@dataclass
class TriageManifest:
    """Manifest for a triage session.

    Created by orchestrator, populated by downloader, read by agent.
    """
    session_type: str = "triage"
    generated_at: str = ""
    data_dir: str = ""  # Relative path from worktree root
    prs: list[PRToReview] = field(default_factory=list)

    def to_dict(self) -> TriageManifestDict:
        """Convert to JSON-serializable dict."""
        return {
            "session_type": self.session_type,
            "generated_at": self.generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data_dir": self.data_dir,
            "prs": [
                {
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.url,
                    "branch": pr.branch,
                    "files": {
                        "diff": pr.files.diff,
                        "metadata": pr.files.metadata,
                    }
                }
                for pr in self.prs
            ]
        }

    @classmethod
    def from_dict(cls, data: TriageManifestDict) -> "TriageManifest":
        """Load from dict."""
        prs = []
        for pr_data in data.get("prs", []):
            files_data = pr_data.get("files", {})
            prs.append(PRToReview(
                number=pr_data["number"],
                title=pr_data["title"],
                url=pr_data["url"],
                branch=pr_data["branch"],
                files=PRFiles(
                    diff=files_data.get("diff", ""),
                    metadata=files_data.get("metadata", ""),
                ),
            ))
        return cls(
            session_type=data.get("session_type", "triage"),
            generated_at=data.get("generated_at", ""),
            data_dir=data.get("data_dir", ""),
            prs=prs,
        )

    def write(self, path: Path) -> None:
        """Write manifest to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        logger.info("[triage] Manifest written: %s (%d PRs)", path, len(self.prs))

    @classmethod
    def read(cls, path: Path) -> "TriageManifest":
        """Read manifest from file."""
        data = json.loads(path.read_text())
        return cls.from_dict(data)

    def get_pr_numbers(self) -> list[int]:
        """Get list of PR numbers for completion handling."""
        return [pr.number for pr in self.prs]
