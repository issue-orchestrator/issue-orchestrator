"""ManifestDownloader port for downloading PR data for tech_lead sessions.

Execution-only: control layer provides manifest and path; adapters fetch data.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.tech_lead_manifest import TechLeadManifest


class ManifestDownloader(Protocol):
    """Protocol for downloading PR data based on a tech_lead manifest."""

    def download(
        self,
        manifest: "TechLeadManifest",
        worktree_path: Path,
    ) -> "TechLeadManifest":
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
        manifest: "TechLeadManifest",
        worktree_path: Path,
    ) -> "TechLeadManifest":
        """Return manifest unchanged."""
        return manifest
