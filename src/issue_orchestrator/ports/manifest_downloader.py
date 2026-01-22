"""ManifestDownloader port for downloading PR data for triage sessions.

Execution-only: control layer provides manifest and path; adapters fetch data.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.triage_manifest import TriageManifest


class ManifestDownloader(Protocol):
    """Protocol for downloading PR data based on a triage manifest."""

    def download(
        self,
        manifest: "TriageManifest",
        worktree_path: Path,
    ) -> "TriageManifest":
        """Fetch all PR data and update manifest with local file paths.

        Args:
            manifest: The manifest with PRs to fetch data for
            worktree_path: Path to the worktree where data should be written

        Returns:
            Updated manifest with file paths populated
        """
        ...


class NullManifestDownloader:
    """ManifestDownloader that does nothing (for tests and defaults)."""

    def download(
        self,
        manifest: "TriageManifest",
        worktree_path: Path,
    ) -> "TriageManifest":
        """Return manifest unchanged."""
        return manifest
