"""Tech Lead manifest builder - creates manifests for tech_lead sessions.

Queries GitHub to find PRs that need tech_lead (carry the tech_lead watch label
but not tech-lead-reviewed or tech-lead-failed labels). The watch label must come
from ``Config.tech_lead_watch_label`` — the single owner shared with the
threshold trigger — and candidate eligibility from
:class:`TechLeadCandidatePolicy` — the single owner shared with threshold fact
gathering — so the PR set that trips the threshold is exactly the set the
session audits.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..domain.tech_lead_manifest import TechLeadManifest, PRToReview, PRFiles
from ..ports import RepositoryHost

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TechLeadCandidatePolicy:
    """Single owner for which watch-labeled PRs are tech_lead batch candidates.

    Threshold fact gathering (``FactGatherer._fetch_tech_lead_prs``) and manifest
    construction (:class:`TechLeadManifestBuilder`) must apply THIS predicate so
    the count that trips a batch and the set the session audits never diverge
    (#6768 round 5: terminally-triaged PRs counted toward the threshold but
    were manifest-filtered, creating endless empty-batch tracking issues).

    A PR stops being a candidate once tech_lead terminalized it
    (``tech_lead_reviewed_label``/``tech_lead_failed_label``) and, on filtered runs,
    when it lies outside the active repository filter label scope.
    """

    tech_lead_reviewed_label: str = "tech-lead-reviewed"
    tech_lead_failed_label: str = "tech-lead-failed"
    required_label: str | None = None

    @classmethod
    def from_config(cls, config: "Config") -> "TechLeadCandidatePolicy":
        """Derive the policy from configuration (custom labels + filter scope)."""
        return cls(
            tech_lead_reviewed_label=config.tech_lead_reviewed_label or "tech-lead-reviewed",
            tech_lead_failed_label=config.tech_lead_failed_label or "tech-lead-failed",
            required_label=config.filtering.label,
        )

    def is_candidate(self, labels: Sequence[str]) -> bool:
        """True when a watch-labeled PR still needs a tech_lead batch review."""
        label_set = set(labels)
        terminalized = bool(
            {self.tech_lead_reviewed_label, self.tech_lead_failed_label} & label_set
        )
        in_scope = self.required_label is None or self.required_label in label_set
        return not terminalized and in_scope


class TechLeadManifestBuilder:
    """Builds tech_lead manifests by querying for PRs that need review."""

    def __init__(
        self,
        repository_host: RepositoryHost,
        watch_label: str = "code-reviewed",
        *,
        candidate_policy: TechLeadCandidatePolicy,
    ):
        self._host = repository_host
        self._watch_label = watch_label
        self._policy = candidate_policy

    def build(self, data_dir: str) -> TechLeadManifest:
        """Build a tech_lead manifest with PRs that need review.

        Args:
            data_dir: Relative path from worktree root where data files will go

        Returns:
            TechLeadManifest with PRs to review (data not yet downloaded)
        """
        prs = self._host.get_prs_with_label(self._watch_label, state="all")
        logger.info(
            "[tech_lead] Found %d PRs with '%s' label",
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
            "[tech_lead] %d PRs need tech_lead review (filtered from %d)",
            len(prs_to_review), len(prs)
        )

        return TechLeadManifest(
            session_type="tech_lead",
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            data_dir=data_dir,
            prs=prs_to_review,
        )
