"""THE owner of externally authored PRs in an integration effort (#9499).

A PR opened off to the side - by the coordinator, the operator, another tool -
is the splice point: once io adopts it, it rides exactly the lanes io's own
PRs ride, and nothing else has to move it.

* **Adoption.** An open, non-draft PR without io's marker that targets the
  effort's branch and references an issue in the effort (open, in this
  engine's scope, labelled ``integrate:<branch>``, with an agent label so io
  can rework it) is adopted: io routes it into its own review lane and marks it
  ``origin:external``. From there the existing lanes own it - review discovery
  puts ``pr-pending`` on the issue and queues the review, the review's
  completion records the issue's awaiting-merge history, rework runs on its
  branch, and the integration owner merges it into the branch once approved,
  under the same gates as io's own work (standing rulings included), so it is
  listed in the delivery. No validated-work record is made: the work lives in
  the PR, and the PR is what every lane follows.
* **Refusal.** A PR into the effort branch that references no issue, or one
  outside the effort, and a PR for an issue in the effort that targets another
  base, are refused: told why on the PR once, and never merged. The merge gate
  (:class:`~.integration_branch.MergeGatekeeper`) re-asks
  :func:`~..domain.external_pr_adoption.merge_provenance` at every merge and
  update, so a review label applied by hand cannot carry an unadopted PR in.

Layering, as for the integration owner: discovery here only READS (one open-PR
listing per general scan, plus the linked issue of a PR into the branch); each
write is an :data:`~..domain.integration_branch.IntegrationStep` the planner
turns into an :class:`~.actions.AdvanceIntegrationAction`, applied through
:func:`~.integration_branch.apply_integration_step`, which asks
:func:`adoption_step_refusal` to re-check on fresh reads and
:func:`write_adoption_step` to write.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

from ..domain.external_pr_adoption import (
    AdoptedExternalPullRequest,
    AdoptExternalPullRequest,
    AdoptionRefusal,
    ExternalStanding,
    ReportRefusedExternalPullRequest,
    adoption_label_writes,
    adoption_obstacle,
    effort_gap,
    external_standing,
    in_effort,
    refusal_marker,
    render_refusal_comment,
)
from ..infra import gh_audit
from ..infra.repo_scope import require_repo
from ..ports.repository_host import RepositoryHostError
from .review_scope import ReviewScopeChecker, extract_issue_number_from_pr, issues_linked

if TYPE_CHECKING:
    from ..domain.integration_branch import IntegrationStep
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

#: The open PRs one scan lists (GitHub's largest page): adoption is not
#: lifecycle-critical, so a PR past it waits for the listing to reach it.
OPEN_PR_LISTING_LIMIT = 100

_Verdict: TypeAlias = "AdoptedExternalPullRequest | AdoptExternalPullRequest | ReportRefusedExternalPullRequest"


@dataclass
class ExternalPullRequestAdoption:
    """One discovery pass over the open PRs (module docstring)."""

    config: "Config"
    host: "RepositoryHost"
    label_manager: "LabelManager"
    #: The label io's review discovery lists PRs by; None without a review lane.
    review_lane: str | None = field(init=False)

    def __post_init__(self) -> None:
        if not self.config.integration.enabled:
            raise RuntimeError("external PRs are adopted only into an enabled integration effort")
        self.review_lane = self.config.code_review_label if self.config.code_review_agent else None

    @property
    def branch(self) -> str:
        return self.config.integration.branch

    def discover(self, state: "OrchestratorState", *, issue_branches: Mapping[int, str]) -> list["IntegrationStep"]:
        """The pass's adoptions and the refusals not yet told; records the board's view."""
        if self.review_lane is None:
            # Without io's review lane an adopted PR could never be reviewed
            # (so never merged): nothing to adopt into.
            logger.debug("Adoption: no review lane configured; external PRs are not adopted")
            return []
        # A PR into another base is reported only for an issue io already holds
        # in the effort: no such PR costs a GitHub read.
        effort_issues = frozenset(
            issue.number for issue in state.cached_scope_issues if in_effort(self.branch, issue.labels)
        )
        try:
            with gh_audit.context(reason=gh_audit.AuditReason.PR_SCAN, scope=gh_audit.AuditScope.PERIODIC):
                prs = self.host.list_prs(state="open", limit=OPEN_PR_LISTING_LIMIT)
            verdicts = [
                verdict for pr in prs
                if (verdict := self._judge(pr, state, issue_branches, effort_issues)) is not None
            ]
        except RepositoryHostError as error:
            # Nothing adopted or refused on a partial read; the next scan reads again.
            logger.warning("Adoption: could not read the open PRs or their issues: %s", error)
            return []
        adoptions = [v for v in verdicts if isinstance(v, AdoptExternalPullRequest)]
        untold = state.external_pull_requests.record_scan(
            (v for v in verdicts if isinstance(v, AdoptedExternalPullRequest)),
            (v for v in verdicts if isinstance(v, ReportRefusedExternalPullRequest)),
        )
        for step in untold:
            logger.warning("Adoption: refused external PR #%d (%s): %s", step.pr_number, step.refusal.value,
                           step.detail)
        return [*adoptions, *untold]

    # -- one PR ------------------------------------------------------------------

    def _judge(
        self, pr: "PRInfo", state: "OrchestratorState", issue_branches: Mapping[int, str],
        effort_issues: frozenset[int],
    ) -> _Verdict | None:
        standing = external_standing(
            branch=self.branch, pr_body=pr.body, pr_branch=pr.branch, base_branch=pr.base_branch,
            draft=pr.draft, pr_labels=pr.labels,
        )
        issue_number = self._linked_issue(pr)
        match standing:
            case ExternalStanding.NOT_EXTERNAL:
                return None
            case ExternalStanding.OTHER_BASE:
                if issue_number not in effort_issues:
                    return None  # not io's business
                return self._refused(
                    pr, issue_number, AdoptionRefusal.WRONG_BASE,
                    f"it targets `{pr.base_branch}`, but issue #{issue_number} integrates on `{self.branch}`",
                )
            case ExternalStanding.ADOPTED:
                # The lanes own it now, and the merge gate re-checks its provenance.
                return AdoptedExternalPullRequest(pr_number=pr.number, issue_number=issue_number, pr_url=pr.url)
            case ExternalStanding.CANDIDATE:
                return self._candidate(pr, issue_number, state, issue_branches)

    def _candidate(
        self, pr: "PRInfo", issue_number: int | None, state: "OrchestratorState",
        issue_branches: Mapping[int, str],
    ) -> AdoptExternalPullRequest | ReportRefusedExternalPullRequest:
        """A PR into the effort branch, not adopted yet: its issue decides."""
        if issue_number is None:
            return self._refused(pr, None, AdoptionRefusal.NO_ISSUE_REFERENCE,
                                 f"it targets `{self.branch}` but references no issue")
        outside = self._outside_effort(pr, issue_number)
        if outside is not None:
            return self._refused(pr, issue_number, AdoptionRefusal.ISSUE_NOT_IN_EFFORT, outside)
        elsewhere = _io_work_elsewhere(pr, issue_number, state, issue_branches)
        if elsewhere is not None:
            return self._refused(pr, issue_number, AdoptionRefusal.IO_WORK_IN_FLIGHT, elsewhere)
        assert self.review_lane is not None  # discover() runs only with a review lane
        return AdoptExternalPullRequest(
            issue_number=issue_number, pr_number=pr.number, pr_url=pr.url, pr_title=pr.title,
            integration_branch=self.branch, review_label=self.review_lane,
        )

    def _linked_issue(self, pr: "PRInfo") -> int | None:
        """The issue every PR lane resolves for *pr*, when the PR really references one.

        Review discovery and the awaiting-merge lanes resolve a PR's issue by
        :func:`extract_issue_number_from_pr`, which falls back to the PR's own
        number; only a reference the PR actually makes counts here.
        """
        repo = require_repo(self.config)
        linked = issues_linked(branch=pr.branch, body=pr.body, repo_slug=repo)
        primary = extract_issue_number_from_pr(pr, repo_slug=repo)
        return primary if primary in linked else None

    def _outside_effort(self, pr: "PRInfo", issue_number: int) -> str | None:
        """Why *issue_number* is not in the effort; None when it is (one issue read)."""
        scope = ReviewScopeChecker(self.config, self.host, log_prefix="ADOPTION", require_open_issue=True)
        decision = scope.check_issue_number(issue_number, pr.number)
        issue = decision.issue
        if not decision.in_scope or issue is None:
            return f"issue #{issue_number} is not open in this engine's scope ({decision.reason})"
        return effort_gap(branch=self.branch, issue_number=issue_number, issue_labels=issue.labels,
                          agent_type=issue.agent_type)

    def _refused(
        self, pr: "PRInfo", issue_number: int | None, refusal: AdoptionRefusal, detail: str,
    ) -> ReportRefusedExternalPullRequest:
        return ReportRefusedExternalPullRequest(
            pr_number=pr.number, pr_url=pr.url, issue_number=issue_number, base_branch=pr.base_branch or "",
            integration_branch=self.branch, refusal=refusal, detail=detail,
        )


def _io_work_elsewhere(
    pr: "PRInfo", issue_number: int, state: "OrchestratorState", issue_branches: Mapping[int, str],
) -> str | None:
    """io's own attempt at the issue, when it has one: two authors never share an issue."""
    active = issue_branches.get(issue_number)
    if active and active != pr.branch:
        return f"io is working issue #{issue_number} on its own branch `{active}`"
    # A completed issue/PR pair is io's published PR, awaiting merge until reconciled.
    others = sorted(
        identity[1] for entry in state.session_history
        if (identity := entry.publication_identity) is not None
        and identity[0] == issue_number and identity[1] != pr.number
    )
    if others:
        return f"io's own PR #{others[0]} for issue #{issue_number} is awaiting merge"
    return None


# -- the writes ------------------------------------------------------------------


def adoption_step_refusal(
    step: "AdoptExternalPullRequest | ReportRefusedExternalPullRequest", host: "RepositoryHost",
) -> str | None:
    """Why *step* must not be written now, on fresh reads; None when it may."""
    try:
        pr = host.get_pr(step.pr_number)
        if isinstance(step, ReportRefusedExternalPullRequest):
            return None if _is_open(pr) else f"PR #{step.pr_number} is no longer open"
        fresh = host.get_issue_labels_fresh(step.issue_number)
    except RepositoryHostError as error:
        return f"PR #{step.pr_number} could not be re-read before it is written: {error}"
    return adoption_obstacle(
        step, pr_open=_is_open(pr), base_branch=pr.base_branch if pr is not None else None,
        pr_body=pr.body if pr is not None else None, issue_labels=fresh,
    )


def _is_open(pr: "PRInfo | None") -> bool:
    return pr is not None and (pr.state or "").strip().lower() == "open"


def write_adoption_step(
    step: "AdoptExternalPullRequest | ReportRefusedExternalPullRequest",
    host: "RepositoryHost",
    labels: "LabelManager",
) -> None:
    """Adopt the PR (review lane first, origin last), or tell it why it was refused."""
    if isinstance(step, ReportRefusedExternalPullRequest):
        if not host.issue_comment_marker_present(step.pr_number, refusal_marker(step.refusal)):
            host.add_comment(step.pr_number, render_refusal_comment(step))
        return
    writes = adoption_label_writes(
        host.get_issue_labels_fresh(step.pr_number), review_label=step.review_label,
        reviewed_label=labels.code_reviewed,
    )
    for name in writes.remove:
        host.remove_label(step.pr_number, name)
    for name in writes.add:
        host.add_label(step.pr_number, name)


__all__ = [
    "ExternalPullRequestAdoption",
    "OPEN_PR_LISTING_LIMIT",
    "adoption_step_refusal",
    "write_adoption_step",
]
