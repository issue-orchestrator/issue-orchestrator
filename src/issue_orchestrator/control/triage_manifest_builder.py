"""Triage manifest builder - creates manifests for triage sessions.

Queries GitHub to find PRs that need triage (have code-reviewed label
but not triage-reviewed or triage-failed labels).
"""

import logging
import time

from .triage_manifest import TriageManifest, PRToReview, PRFiles
from ..ports import RepositoryHost

logger = logging.getLogger(__name__)


class TriageManifestBuilder:
    """Builds triage manifests by querying for PRs that need review."""

    def __init__(
        self,
        repository_host: RepositoryHost,
        code_reviewed_label: str = "code-reviewed",
        triage_reviewed_label: str = "triage-reviewed",
        triage_failed_label: str = "triage-failed",
    ):
        self._host = repository_host
        self._code_reviewed_label = code_reviewed_label
        self._triage_reviewed_label = triage_reviewed_label
        self._triage_failed_label = triage_failed_label

    def build(self, data_dir: str) -> TriageManifest:
        """Build a triage manifest with PRs that need review.

        Args:
            data_dir: Relative path from worktree root where data files will go

        Returns:
            TriageManifest with PRs to review (data not yet downloaded)
        """
        # Find PRs with code-reviewed label
        prs = self._host.get_prs_with_label(self._code_reviewed_label, state="all")
        logger.info(
            "[triage] Found %d PRs with '%s' label",
            len(prs), self._code_reviewed_label
        )

        # Filter out already-triaged PRs
        prs_to_review = []
        for pr in prs:
            if self._triage_reviewed_label in pr.labels:
                logger.debug("[triage] Skipping PR #%d (already triaged)", pr.number)
                continue
            if self._triage_failed_label in pr.labels:
                logger.debug("[triage] Skipping PR #%d (triage failed)", pr.number)
                continue

            prs_to_review.append(PRToReview(
                number=pr.number,
                title=pr.title,
                url=pr.url,
                branch=pr.branch,
                files=PRFiles(),
            ))

        logger.info(
            "[triage] %d PRs need triage review (filtered from %d)",
            len(prs_to_review), len(prs)
        )

        return TriageManifest(
            session_type="triage",
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            data_dir=data_dir,
            prs=prs_to_review,
        )
