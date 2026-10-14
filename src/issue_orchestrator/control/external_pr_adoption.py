"""THE owner of externally authored PRs in an integration effort (#9499).

A PR opened off to the side - by the coordinator, the operator, another tool -
is the splice point: once io adopts it, it rides exactly the lanes io's own
PRs ride, and nothing else has to move it.

* **Adoption.** An external PR that THE admission rule
  (:func:`~..domain.external_pr_adoption.judge_external_pull_request`) finds
  adoptable is routed into io's own review lane and marked
  ``origin:external``. From there the existing lanes own it - review discovery
  puts ``pr-pending`` on the issue and queues the review, the review's
  completion records the issue's awaiting-merge history, rework runs on its
  branch, and the integration owner merges it into the branch once approved,
  under the same gates as io's own work (standing rulings included), so it is
  listed in the delivery. No validated-work record is made: the work lives in
  the PR, and the PR is what every lane follows.
* **Refusal.** A PR the rule refuses is told why on the PR once per reason,
  and never merged.
* **One rule, every boundary.** :class:`ExternalPullRequestGate` reads the
  facts the rule judges - the PR, the issue it references (complete scope),
  and io's own hold on that issue (:class:`~..domain.external_pr_adoption.IoCustody`,
  from an :class:`IoCustodyReader`) - and is asked by discovery here, by the
  integration owner's routing of an approved PR (rework, update, merge), and
  by the applier's re-check before every write (adoption, refusal report,
  return to review, update, merge), each time on that boundary's own reads.
* **Reviewed-head custody.** :meth:`ExternalPullRequestGate.head_reviewed`
  asks for io's review receipt for the PR's CURRENT head
  (:func:`~..domain.external_pr_adoption.reviewed_head_receipt`, an exact
  comment written under io's own credential when io's reviewer approves).

Layering, as for the integration owner: discovery here only READS (one
complete open-PR listing per general scan, plus the issue each PR into the
branch references); each write is an
:data:`~..domain.integration_branch.IntegrationStep` the planner turns into an
:class:`~.actions.AdvanceIntegrationAction`, applied through
:func:`~.integration_branch.apply_integration_step`, which asks
:func:`external_step_refusal` to re-judge on fresh reads and
:func:`~.external_pr_adoption_writes.write_external_step` to write.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeAlias

from ..domain.external_pr_adoption import (
    Admission,
    AdoptedExternalPullRequest,
    AdoptExternalPullRequest,
    ExternalAdmission,
    ExternalPullRequestFacts,
    IoCustody,
    IssueLink,
    LinkedIssueFacts,
    PullRequestFacts,
    ReportRefusedExternalPullRequest,
    ReturnExternalPullRequestToReview,
    external_work,
    in_effort,
    judge_external_pull_request,
    merge_refusal,
    refusal_marker,
    reviewed_head_receipt,
)
from ..domain.models import Issue as IssueSnapshot
from ..infra import gh_audit
from ..infra.repo_scope import require_repo
from ..ports.repository_host import RepositoryHostError
from .issue_scope import evaluate_issue_scope
from .review_scope import extract_issue_number_from_pr, issues_linked

if TYPE_CHECKING:
    from ..domain.external_pr_adoption import ExternalPullRequestLedger
    from ..domain.integration_branch import IntegrationStep
    from ..domain.models import OrchestratorState, SessionHistoryEntry
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)

_Verdict: TypeAlias = "AdoptExternalPullRequest | ReportRefusedExternalPullRequest | None"


# -- the facts -------------------------------------------------------------------


def issue_link(pr: "PRInfo", *, repo_slug: str) -> IssueLink:
    """Every issue *pr*'s branch and body reference.

    Review discovery and the awaiting-merge lanes resolve a PR's issue by
    :func:`extract_issue_number_from_pr` (its branch first, then its body,
    then the PR's own number); a PR that references something is linked to
    exactly one issue only when that is all it references and the issue the
    lanes resolve. A branch naming one issue and a body another, or a body
    closing two, is ambiguous - never reduced to the lanes' first pick.
    """
    linked = issues_linked(branch=pr.branch, body=pr.body, repo_slug=repo_slug)
    if not linked:
        return IssueLink(frozenset())
    primary = extract_issue_number_from_pr(pr, repo_slug=repo_slug)
    return IssueLink(linked | {primary})


def pull_request_facts(pr: "PRInfo", *, repo_slug: str, labels: Iterable[str]) -> PullRequestFacts:
    """*pr* as the admission rule reads it, with *labels* as the boundary read them."""
    return PullRequestFacts(
        number=pr.number, open=(pr.state or "").strip().lower() == "open", draft=bool(pr.draft),
        body=pr.body or "", branch=pr.branch, base_branch=pr.base_branch, labels=frozenset(labels),
        link=issue_link(pr, repo_slug=repo_slug),
    )


def linked_issue_facts(config: "Config", issue: "Issue", *, labels: Iterable[str]) -> LinkedIssueFacts:
    """*issue* as the admission rule reads it, with *labels* as the boundary read
    them: the engine's COMPLETE scope (open, milestone, label filters,
    single-issue scope)."""
    snapshot = IssueSnapshot(
        number=issue.number, title=issue.title, labels=list(labels), state=issue.state, milestone=issue.milestone,
    )
    scope = evaluate_issue_scope(
        config, snapshot, require_open=True, include_milestone_filter=True, include_issue_number_filter=True,
    )
    agent = snapshot.agent_type
    return LinkedIssueFacts(
        number=issue.number, scope_gap=None if scope.in_scope else (scope.detail or scope.code),
        labels=frozenset(snapshot.labels), agent_type=agent,
        # The rework launcher's own rule: an agent this engine has a config for.
        agent_configured=agent is not None and bool(config.agents.get(agent)),
    )


def io_attempts(prs: Iterable["PRInfo"], *, repo_slug: str) -> dict[int, frozenset[int]]:
    """Per issue: the open PRs that are io's own or adopted attempts at it."""
    attempts: dict[int, set[int]] = {}
    for pr in prs:
        facts = pull_request_facts(pr, repo_slug=repo_slug, labels=pr.labels)
        if facts.open and (facts.io_owned or facts.adopted):
            for issue_number in facts.link.issues:  # an ambiguous attempt holds every issue it names
                attempts.setdefault(issue_number, set()).add(pr.number)
    return {number: frozenset(numbers) for number, numbers in attempts.items()}


def published_attempts(history: Iterable["SessionHistoryEntry"], issue_number: int) -> frozenset[int]:
    """io's published PRs for the issue, awaiting merge until reconciled."""
    return frozenset(
        identity[1] for entry in history
        if (identity := entry.publication_identity) is not None and identity[0] == issue_number
    )


class IoCustodyReader(Protocol):
    """io's own hold on an issue, as one boundary reads it."""

    def custody(self, issue_number: int) -> IoCustody: ...


@dataclass(frozen=True)
class ScanCustody:
    """Discovery's view: io's worktrees, its published history, and the scan's
    own complete open-PR listing."""

    issue_branches: Mapping[int, str]
    history: Sequence["SessionHistoryEntry"]
    attempts: Mapping[int, frozenset[int]]

    def custody(self, issue_number: int) -> IoCustody:
        return IoCustody(
            working_branch=self.issue_branches.get(issue_number),
            pull_requests=self.attempts.get(issue_number, frozenset()) | published_attempts(self.history, issue_number),
        )


@dataclass(frozen=True)
class StateCustody:
    """The routing's view between scans: io's live sessions, its published
    history, and the attempts the last complete scan listed."""

    state: "OrchestratorState"

    def custody(self, issue_number: int) -> IoCustody:
        return IoCustody(
            working_branch=session_branch(self.state, issue_number),
            pull_requests=(self.state.external_pull_requests.attempts_at(issue_number)
                           | published_attempts(self.state.session_history, issue_number)),
        )


def session_branch(state: "OrchestratorState", issue_number: int) -> str | None:
    """The branch a live io session works *issue_number* on, if one does."""
    return next(
        (session.branch_name for session in state.active_sessions if session.issue.number == issue_number), None,
    )


@dataclass
class FreshCustody:
    """A write's view: io's live sessions and published history, and a FRESH
    complete open-PR listing (read once, on first use)."""

    host: "RepositoryHost"
    repo_slug: str
    active_branch: Callable[[int], str | None]
    history: Callable[[], Sequence["SessionHistoryEntry"]]
    _attempts: dict[int, frozenset[int]] | None = field(default=None, init=False)

    def custody(self, issue_number: int) -> IoCustody:
        if self._attempts is None:
            self._attempts = io_attempts(self.host.list_open_prs_complete(), repo_slug=self.repo_slug)
        return IoCustody(
            working_branch=self.active_branch(issue_number),
            pull_requests=self._attempts.get(issue_number, frozenset()) | published_attempts(self.history(), issue_number),
        )


# -- THE gate ----------------------------------------------------------------------


@dataclass
class ExternalPullRequestGate:
    """THE admission rule and reviewed-head custody, on one boundary's reads.

    ``ledger`` memoizes found review receipts between scans (the routing);
    None at a write, which always re-reads the receipt.
    """

    config: "Config"
    host: "RepositoryHost"
    custody: IoCustodyReader
    ledger: "ExternalPullRequestLedger | None" = None

    @property
    def branch(self) -> str:
        return self.config.integration.branch

    @property
    def repo_slug(self) -> str:
        return require_repo(self.config)

    def facts(self, pr: "PRInfo", *, pr_labels: Iterable[str]) -> PullRequestFacts:
        return pull_request_facts(pr, repo_slug=self.repo_slug, labels=pr_labels)

    def judge(self, pr: "PRInfo", *, pr_labels: Iterable[str]) -> ExternalAdmission:
        """THE admission rule for *pr*, reading the issue it references (one read)."""
        facts = self.facts(pr, pr_labels=pr_labels)
        issue = self._read_issue(facts.linked_issue) if facts.linked_issue is not None else None
        custody = self.custody.custody(facts.linked_issue) if facts.linked_issue is not None else IoCustody(None, frozenset())
        return judge_external_pull_request(self.branch, ExternalPullRequestFacts(pr=facts, issue=issue, custody=custody))

    def refusal(
        self, *, pr: "PRInfo", issue: "Issue", issue_labels: Iterable[str], pr_labels: Iterable[str],
    ) -> str | None:
        """Why io may not rework, update or merge *pr* as *issue*'s PR; None when it may."""
        facts = self.facts(pr, pr_labels=pr_labels)
        if facts.io_owned:
            return None
        return merge_refusal(
            self.branch, facts, issue=linked_issue_facts(self.config, issue, labels=issue_labels),
            custody=self.custody.custody(issue.number),
        )

    def head_reviewed(self, pr: "PRInfo", *, pr_labels: Iterable[str]) -> bool:
        """Whether io's review approved exactly *pr*'s current head (io's own PR: its lanes own that)."""
        facts = self.facts(pr, pr_labels=pr_labels)
        if facts.io_owned:
            return True
        head = pr.head_sha
        if not head:
            return False
        if self.ledger is not None and self.ledger.reviewed_head_known(pr.number, head):
            return True
        found = self.host.find_issue_comment_receipt(pr.number, body=reviewed_head_receipt(pr.number, head)) is not None
        if found and self.ledger is not None:
            self.ledger.remember_reviewed_head(pr.number, head)
        return found

    def _read_issue(self, number: int) -> LinkedIssueFacts:
        issue = self.host.get_issue(number)
        if issue is None:
            # A reference to an issue that does not exist: nothing io could adopt it for.
            return LinkedIssueFacts(
                number=number, scope_gap="it does not exist", labels=frozenset(), agent_type=None,
                agent_configured=False,
            )
        return linked_issue_facts(self.config, issue, labels=issue.labels)


def review_lane(config: "Config") -> str | None:
    """The label io's review discovery lists PRs by; None without a review lane."""
    return config.code_review_label if config.code_review_agent else None


def routing_gate(config: "Config", host: "RepositoryHost", state: "OrchestratorState") -> ExternalPullRequestGate:
    """The integration owner's gate between scans: io's live state and the last
    complete scan's attempts; found receipts memoized in the ledger."""
    return ExternalPullRequestGate(config, host, StateCustody(state), ledger=state.external_pull_requests)


def write_gate(
    config: "Config", host: "RepositoryHost", state: Callable[[], "OrchestratorState"],
) -> ExternalPullRequestGate:
    """A write's gate: io's live state and a FRESH complete listing; every
    receipt re-read."""
    return ExternalPullRequestGate(config, host, FreshCustody(
        host, require_repo(config), active_branch=lambda number: session_branch(state(), number),
        history=lambda: state().session_history,
    ))


# -- discovery ---------------------------------------------------------------------


@dataclass
class ExternalPullRequestAdoption:
    """One discovery pass over every open PR (module docstring)."""

    config: "Config"
    host: "RepositoryHost"

    def __post_init__(self) -> None:
        if not self.config.integration.enabled:
            raise RuntimeError("external PRs are adopted only into an enabled integration effort")

    @property
    def branch(self) -> str:
        return self.config.integration.branch

    @property
    def review_lane(self) -> str | None:
        return review_lane(self.config)

    def discover(self, state: "OrchestratorState", *, issue_branches: Mapping[int, str]) -> list["IntegrationStep"]:
        """The pass's adoptions and the refusals not yet told; records the board's view."""
        if self.review_lane is None:
            # Without io's review lane an adopted PR could never be reviewed
            # (so never merged): nothing to adopt into.
            logger.debug("Adoption: no review lane configured; external PRs are not adopted")
            return []
        repo = require_repo(self.config)
        # A PR into another base is reported only for an issue io already holds
        # in the effort: no such PR costs a GitHub read.
        effort_issues = frozenset(
            issue.number for issue in state.cached_scope_issues if in_effort(self.branch, issue.labels)
        )
        try:
            with gh_audit.context(reason=gh_audit.AuditReason.PR_SCAN, scope=gh_audit.AuditScope.PERIODIC):
                # Complete or an error, never a page: a PR past any page would
                # wait forever, and the board's view is replaced only by a whole scan.
                prs = self.host.list_open_prs_complete()
            attempts = io_attempts(prs, repo_slug=repo)
            gate = ExternalPullRequestGate(
                self.config, self.host, ScanCustody(issue_branches, tuple(state.session_history), attempts),
            )
            verdicts = [self._verdict(pr, gate, effort_issues) for pr in prs]
        except RepositoryHostError as error:
            # Nothing adopted or refused on a partial read; the next scan reads again.
            logger.warning("Adoption: could not read the open PRs or their issues: %s", error)
            return []
        adoptions = [verdict for verdict in verdicts if isinstance(verdict, AdoptExternalPullRequest)]
        ledger = state.external_pull_requests
        unacknowledged = ledger.record_scan(
            (AdoptedExternalPullRequest(pr_number=pr.number, issue_number=facts.linked_issue, pr_url=pr.url)
             for pr in prs if (facts := gate.facts(pr, pr_labels=pr.labels)).adopted),
            (verdict for verdict in verdicts if isinstance(verdict, ReportRefusedExternalPullRequest)),
            attempts,
        )
        untold = [step for step in unacknowledged if not self._seen_on_pr(step, ledger)]
        for step in untold:
            logger.warning("Adoption: refused external PR #%d (%s): %s", step.pr_number, step.refusal.value,
                           step.detail)
        return [*adoptions, *untold]

    def _seen_on_pr(self, step: ReportRefusedExternalPullRequest, ledger: "ExternalPullRequestLedger") -> bool:
        """Whether *step*'s report is already on the PR - acknowledged then, and
        only then: a report whose write failed is planned again next scan."""
        try:
            seen = self.host.issue_comment_marker_present(step.pr_number, refusal_marker(step))
        except RepositoryHostError as error:
            logger.warning("Adoption: PR #%d's refusal reports unreadable: %s", step.pr_number, error)
            return False  # planned: the write re-reads the marker before it comments
        if seen:
            ledger.acknowledge(step)
        return seen

    def _verdict(self, pr: "PRInfo", gate: ExternalPullRequestGate, effort_issues: frozenset[int]) -> _Verdict:
        facts = gate.facts(pr, pr_labels=pr.labels)
        if not external_work(self.branch, facts):
            return None  # io's own, a draft, the delivery PR: no issue read
        if facts.base_branch != self.branch and not facts.link.issues & effort_issues:
            return None  # another base, for an issue io does not hold in the effort: not io's business
        admission = gate.judge(pr, pr_labels=pr.labels)
        return external_step(pr, admission, branch=self.branch, review_label=self._lane())

    def _lane(self) -> str:
        lane = self.review_lane
        assert lane is not None, "discover() runs only with a review lane"
        return lane


def external_step(
    pr: "PRInfo", admission: ExternalAdmission, *, branch: str, review_label: str,
) -> "AdoptExternalPullRequest | ReportRefusedExternalPullRequest | None":
    """The write THE admission rule's verdict asks for: an adoption, a refusal report, or none."""
    if admission.admission is Admission.ADOPTABLE:
        assert admission.issue_number is not None
        return AdoptExternalPullRequest(
            issue_number=admission.issue_number, pr_number=pr.number, pr_url=pr.url, pr_title=pr.title,
            integration_branch=branch, review_label=review_label,
        )
    return refusal_report(pr, admission, branch=branch)


def refusal_report(
    pr: "PRInfo", admission: ExternalAdmission, *, branch: str,
) -> ReportRefusedExternalPullRequest | None:
    """The refusal report THE admission rule's verdict asks for; None unless it refused."""
    if admission.refusal is None:
        return None
    return ReportRefusedExternalPullRequest(
        pr_number=pr.number, pr_url=pr.url, issue_number=admission.issue_number,
        base_branch=pr.base_branch or "", integration_branch=branch, refusal=admission.refusal,
        detail=admission.detail,
    )


# -- the write re-check ----------------------------------------------------------


ExternalStep: TypeAlias = (
    "AdoptExternalPullRequest | ReportRefusedExternalPullRequest | ReturnExternalPullRequestToReview"
)


def external_step_refusal(step: ExternalStep, gate: ExternalPullRequestGate) -> str | None:
    """Why *step* must not be written now, re-judged by THE admission rule on
    fresh reads; None when it may."""
    try:
        pr = gate.host.get_pr(step.pr_number)
        if pr is None:
            return f"PR #{step.pr_number} is gone"
        pr_labels = gate.host.get_issue_labels_fresh(step.pr_number)
        admission = gate.judge(pr, pr_labels=pr_labels)
        if isinstance(step, ReturnExternalPullRequestToReview):
            return _return_refusal(step, pr, admission, gate=gate, pr_labels=pr_labels)
    except RepositoryHostError as error:
        return f"PR #{step.pr_number} could not be re-read before it is written: {error}"
    if isinstance(step, AdoptExternalPullRequest):
        if admission.admission is Admission.ADOPTABLE and admission.issue_number == step.issue_number:
            return None
        return f"PR #{step.pr_number} is no longer adoptable for issue #{step.issue_number}: {_said(admission)}"
    fresh = refusal_report(pr, admission, branch=step.integration_branch)
    if fresh is not None and refusal_marker(fresh) == refusal_marker(step):
        return None  # the same refusal - reason, issue, bases and detail - still holds
    return f"PR #{step.pr_number} is no longer refused as planned ({step.detail}): {_said(admission)}"


def _return_refusal(
    step: ReturnExternalPullRequestToReview, pr: "PRInfo", admission: ExternalAdmission, *,
    gate: ExternalPullRequestGate, pr_labels: Sequence[str],
) -> str | None:
    if pr.head_sha != step.head_sha:
        return f"PR #{step.pr_number} head moved since it was judged; re-checked next pass"
    if admission.admission is not Admission.ADMITTED or admission.issue_number != step.issue_number:
        return f"PR #{step.pr_number} is not an admitted PR of issue #{step.issue_number}: {_said(admission)}"
    approved = gate.head_reviewed(pr, pr_labels=pr_labels)
    return f"io's review approved PR #{step.pr_number} at {step.head_sha[:8]} meanwhile" if approved else None


def _said(admission: ExternalAdmission) -> str:
    return admission.detail or admission.admission.value


__all__ = [
    "ExternalPullRequestAdoption",
    "ExternalPullRequestGate",
    "ExternalStep",
    "FreshCustody",
    "IoCustodyReader",
    "ScanCustody",
    "StateCustody",
    "external_step",
    "external_step_refusal",
    "io_attempts",
    "issue_link",
    "linked_issue_facts",
    "pull_request_facts",
    "refusal_report",
    "review_lane",
    "routing_gate",
    "session_branch",
    "write_gate",
]
