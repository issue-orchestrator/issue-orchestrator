"""Tech Lead manifest downloader - fetches PR data for tech_lead sessions.

Takes a TechLeadManifest and fetches the actual data (diffs, metadata)
from GitHub, writing files to the session directory.

This is an adapter implementing the ManifestDownloader port.
"""

import json
import logging
from pathlib import Path

from ..domain.tech_lead_manifest import TechLeadManifest, PRFiles
from ..ports import RepositoryHost, CommandRunner

logger = logging.getLogger(__name__)


class TechLeadDownloader:
    """Downloads PR data based on a tech_lead manifest.

    Implements ManifestDownloader port.
    Uses RepositoryHost for PR metadata and CommandRunner for diffs
    (since diff isn't in the protocol yet).
    """

    def __init__(
        self,
        repository_host: RepositoryHost,
        command_runner: CommandRunner,
    ):
        self._host = repository_host
        self._runner = command_runner

    def download(self, manifest: TechLeadManifest, worktree_path: Path) -> TechLeadManifest:
        """Fetch all PR data and update manifest with local file paths.

        Args:
            manifest: The manifest with PRs to fetch data for
            worktree_path: Path to the worktree where data should be written

        Returns:
            Updated manifest with file paths populated
        """
        if not manifest.data_dir:
            raise ValueError("Manifest data_dir must be set before downloading")

        data_path = worktree_path / manifest.data_dir
        data_path.mkdir(parents=True, exist_ok=True)

        for pr in manifest.prs:
            try:
                pr.files = self._download_pr_data(pr.number, data_path)
                logger.info("[tech_lead] Downloaded data for PR #%d", pr.number)
            except Exception as e:
                logger.warning("[tech_lead] Failed to download PR #%d: %s", pr.number, e)
                # Continue with other PRs even if one fails

        return manifest

    def _download_pr_data(self, pr_number: int, data_path: Path) -> PRFiles:
        """Download diff and metadata for a single PR."""
        # Fetch and write diff using gh CLI
        diff_filename = f"pr-{pr_number}-diff.txt"
        diff_path = data_path / diff_filename
        diff_result = self._runner.run(["gh", "pr", "diff", str(pr_number)])
        if diff_result.returncode == 0:
            diff_path.write_text(diff_result.stdout)
        else:
            diff_path.write_text(f"# Error fetching diff: {diff_result.stderr}")

        # Fetch and write metadata via RepositoryHost
        meta_filename = f"pr-{pr_number}-meta.json"
        meta_path = data_path / meta_filename
        pr = self._host.get_pr(pr_number)
        if pr:
            metadata = {
                "number": pr.number,
                "title": pr.title,
                "body": pr.body or "",
                "branch": pr.branch,
                "url": pr.url,
                "state": pr.state,
                "labels": pr.labels,
            }
        else:
            metadata = {"error": f"PR #{pr_number} not found"}
        meta_path.write_text(json.dumps(metadata, indent=2))

        return PRFiles(diff=diff_filename, metadata=meta_filename)
