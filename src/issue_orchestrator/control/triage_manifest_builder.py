"""Triage manifest builder - creates manifests for triage sessions.

Queries GitHub to find PRs that need triage (carry the triage watch label
but not triage-reviewed or triage-failed labels). The watch label must come
from ``Config.triage_watch_label`` — the single owner shared with the
threshold trigger — and candidate eligibility from
:class:`TriageCandidatePolicy` — the single owner shared with threshold fact
gathering — so the PR set that trips the threshold is exactly the set the
session audits.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..domain.triage_manifest import TriageManifest, PRToReview, PRFiles
from ..ports import RepositoryHost

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriageCandidatePolicy:
    """Single owner for which watch-labeled PRs are triage batch candidates.

    Threshold fact gathering (``FactGatherer._fetch_triage_prs``) and manifest
    construction (:class:`TriageManifestBuilder`) must apply THIS predicate so
    the count that trips a batch and the set the session audits never diverge
    (#6768 round 5: terminally-triaged PRs counted toward the threshold but
    were manifest-filtered, creating endless empty-batch tracking issues).

    A PR stops being a candidate once triage terminalized it
    (``triage_reviewed_label``/``triage_failed_label``) and, on filtered runs,
    when it lies outside the active repository filter label scope.
    """

    triage_reviewed_label: str = "triage-reviewed"
    triage_failed_label: str = "triage-failed"
    required_label: str | None = None

    @classmethod
    def from_config(cls, config: "Config") -> "TriageCandidatePolicy":
        """Derive the policy from configuration (custom labels + filter scope)."""
        return cls(
            triage_reviewed_label=config.triage_reviewed_label or "triage-reviewed",
            triage_failed_label=config.triage_failed_label or "triage-failed",
            required_label=config.filtering.label,
        )

    def is_candidate(self, labels: Sequence[str]) -> bool:
        """True when a watch-labeled PR still needs a triage batch review."""
        label_set = set(labels)
        terminalized = bool(
            {self.triage_reviewed_label, self.triage_failed_label} & label_set
        )
        in_scope = self.required_label is None or self.required_label in label_set
        return not terminalized and in_scope


class TriageManifestBuilder:
    """Builds triage manifests by querying for PRs that need review."""

    def __init__(
        self,
        repository_host: RepositoryHost,
        watch_label: str = "code-reviewed",
        *,
        candidate_policy: TriageCandidatePolicy,
    ):
        self._host = repository_host
        self._watch_label = watch_label
        self._policy = candidate_policy

    def build(self, data_dir: str) -> TriageManifest:
        """Build a triage manifest with PRs that need review.

        Args:
            data_dir: Relative path from worktree root where data files will go

        Returns:
            TriageManifest with PRs to review (data not yet downloaded)
        """
        prs = self._host.get_prs_with_label(self._watch_label, state="all")
        logger.info(
            "[triage] Found %d PRs with '%s' label",
            len(prs), self._watch_label
        )

        # Filter through the shared candidate owner (already-triaged PRs and,
        # on filtered runs, PRs outside the filter label scope drop out).
        prs_to_review = [
            PRToReview(
                number=pr.number,
                title=pr.title,
                url=pr.url,
                branch=pr.branch,
                files=PRFiles(),
            )
            for pr in prs
            if self._policy.is_candidate(pr.labels)
        ]

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
