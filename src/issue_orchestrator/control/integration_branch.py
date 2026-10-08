"""THE owner of integration-branch mode (#8144).

On a repository whose default branch is protected, the operator merging every
PR is the bottleneck, and every merge moves the base under the open PRs so each
sibling spends an agent rework cycle on conflicts (porchpin PR #379 spent 9 of
10 cycles mostly on rebases). In integration mode agents' PRs target a
long-lived integration branch, and this owner does what the coordinator did by
hand (``.coord/integration_merger.sh``):

* **It merges approved PRs into integration itself**, serialized - at most one
  merge per pass, and only while the tip the PR was checked against is still
  the tip - once every gate holds: reviewer approval (or the batch tech-lead
  review, ``merge_after``), checks green on a head that already CONTAINS the
  integration tip, no person's hold on the issue or PR (``needs-human`` of
  either scope, #7678), and no standing ruling on the issue (#8141): a PR whose
  issue carries one is handed to a person to check against it instead.
* **It brings an approved PR up to the integration tip mechanically** before
  any agent rework: GitHub merges the tip into the PR branch server-side
  (guarded by the expected head). No session runs and no rework cycle is
  spent. Only a real conflict (GitHub reports the PR ``dirty``) or a failed
  check goes to agent rework, through the awaiting-merge reconciler's
  existing path.
* **It keeps the branch**: creates it from the default branch when missing,
  fast-forwards it to the default branch after the operator merges the
  delivery PR, merges the default branch in when both moved on, and maintains
  ONE delivery PR (integration -> default branch) whose generated body lists
  the PRs it delivers. The Tech lead page shows that PR as waiting on the
  operator.

Layering, as for the merge queue coordinator: discovery here only READS the
repository host; every write is an :data:`~..domain.integration_branch.IntegrationStep`
fact that the planner turns into an :class:`~.actions.AdvanceIntegrationAction`
and the applier executes through :func:`apply_integration_step`, which
re-checks what can change between discovery and the write. The issue a merged
PR resolves is closed by the reconciler's close-on-merge path (GitHub closes
issues only on default-branch merges).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from collections.abc import Sequence
from typing import TYPE_CHECKING, Callable

from ..domain.integration_branch import (
    BranchMergeOutcome,
    CreateIntegrationBranch,
    FastForwardIntegration,
    IntegrationDeliveryView,
    IntegrationStep,
    MergeIntoIntegration,
    OpenDeliveryPullRequest,
    RefreshDeliveryPullRequest,
    SyncIntegrationFromDefault,
    UpdatePullRequestBranch,
    delivered_tip,
    delivery_title,
    describe_step,
    merge_commit_message,
    merged_in_delivery,
    render_delivery_body,
)
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import make_trace_event
from ..ports.repository_host import RepositoryHostError
from ..ports.standing_rulings import StandingRulingsUnavailable
from .awaiting_merge_post_publish_policy import PostApprovalAction, build_escalation
from .human_gates import holds_merge

if TYPE_CHECKING:
    from ..domain.models import DiscoveredAwaitingMergeEscalation, OrchestratorState
    from ..events import EventContext
    from ..infra.config_models import IntegrationConfig
    from ..ports import EventSink
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost
    from ..ports.standing_rulings import StandingRulings
    from .action_results import ActionResult
    from .actions import AdvanceIntegrationAction
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

#: How often the branch upkeep (heads, comparison, delivery PR) runs at most.
INTEGRATION_UPKEEP_INTERVAL_SECONDS = 60.0


class IntegrationConfigError(RuntimeError):
    """Integration mode is configured in a way that cannot work on this repository."""


@dataclass(frozen=True)
class IntegrationRouting:
    """What the owner made of one approved PR's post-approval state.

    ``action`` is what the reconciler's default dispatch does next (a real
    conflict or failed check -> rework, branch protection -> escalation,
    pending checks -> the timeout machine; anything else -> nothing). The
    owner's own steps (merge, mechanical update) were recorded on the owner.
    ``escalation`` is the hand-over to a person (a standing ruling to check).
    """

    action: PostApprovalAction
    escalation: "DiscoveredAwaitingMergeEscalation | None" = None


#: The routing that leaves the PR alone this pass.
_NOTHING: PostApprovalAction = "UNKNOWN"


@dataclass
class IntegrationBranchOwner:
    """One discovery pass of integration mode (module docstring).

    Built per pass: :meth:`discovered_steps` hands over the pass's facts (one
    merge at most), and the integration tip is read once per pass.
    """

    config: "IntegrationConfig"
    host: "RepositoryHost"
    label_manager: "LabelManager"
    standing_rulings: "StandingRulings"
    clock: Callable[[], float]
    steps: list[IntegrationStep] = field(default_factory=list)
    _tip: str | None = field(default=None, init=False)
    _tip_read: bool = field(default=False, init=False)
    _merge_candidates: list[MergeIntoIntegration] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not self.config.enabled:
            raise IntegrationConfigError("the integration owner runs only when integration.enabled")

    @property
    def branch(self) -> str:
        return self.config.branch

    def owns(self, pr: "PRInfo") -> bool:
        """Whether *pr* targets the integration branch (only those are io's to merge)."""
        return pr.base_branch == self.branch

    def gate_label(self) -> str:
        """The PR label required before io merges (``merge_after``)."""
        if self.config.merge_after == "tech-lead-reviewed":
            return self.label_manager.tech_lead_reviewed
        return self.label_manager.code_reviewed

    # -- one approved PR --------------------------------------------------------

    def route(self, *, pr: "PRInfo", issue: "Issue", action: PostApprovalAction) -> IntegrationRouting:
        """Decide what happens to one reviewer-approved PR into integration."""
        if action in ("REWORK_CONFLICT", "REWORK_CHECK_FAILED"):
            # A real conflict or a failed check: only an agent can fix it.
            return IntegrationRouting(action)
        if not self._io_may_act(pr, issue):
            return IntegrationRouting(_NOTHING)  # a person decides first, or the gate is not passed
        if action == "BLOCKED_TERMINAL":
            return IntegrationRouting(action)  # protection on integration: a person
        if action not in ("READY", "WAIT_FOR_CHECKS", "REWORK_BEHIND"):
            return IntegrationRouting(_NOTHING)  # mergeability not known yet
        tip = self._integration_tip()
        head = pr.head_sha
        if tip is None or not head:
            return IntegrationRouting(_NOTHING)  # the upkeep creates the branch
        return self._route_against_tip(pr=pr, issue=issue, action=action, head=head, tip=tip)

    def _io_may_act(self, pr: "PRInfo", issue: "Issue") -> bool:
        """No person's hold on the issue or PR (#7678), and the PR passed ``merge_after``."""
        held = holds_merge(self.label_manager, issue.labels, pr.labels)
        return not held and self.gate_label() in pr.labels

    def _route_against_tip(
        self, *, pr: "PRInfo", issue: "Issue", action: PostApprovalAction, head: str, tip: str
    ) -> IntegrationRouting:
        try:
            current = self.host.compare_commits(tip, head).contains_base
        except RepositoryHostError as error:
            logger.warning("Integration: cannot compare PR #%d with %s: %s", pr.number, self.branch, error)
            return IntegrationRouting(_NOTHING)
        if not current:
            # Behind the tip: bring it up mechanically; its checks then run on
            # a head that contains the tip. Never an agent rework cycle.
            self.steps.append(UpdatePullRequestBranch(
                issue_number=issue.number, pr_number=pr.number, head_sha=head, integration_tip=tip,
            ))
            return IntegrationRouting(_NOTHING)
        if action == "WAIT_FOR_CHECKS":
            return IntegrationRouting(action)  # checks running on a current head
        if action != "READY":
            return IntegrationRouting(_NOTHING)  # GitHub still says behind: re-read
        return self._merge_or_hand_over(pr=pr, issue=issue, head=head, tip=tip)

    def _merge_or_hand_over(self, *, pr: "PRInfo", issue: "Issue", head: str, tip: str) -> IntegrationRouting:
        try:
            rulings = self.standing_rulings.active(issue.number)
        except StandingRulingsUnavailable as error:
            logger.warning(issue_log(issue.number, "Integration: rulings unreadable, PR #%d not merged: %s"),
                           pr.number, error)
            return IntegrationRouting(_NOTHING)
        if rulings:
            ids = ", ".join(ruling.ruling_id for ruling in rulings)
            return IntegrationRouting(_NOTHING, escalation=build_escalation(
                pr=pr, issue_number=issue.number, issue_key=issue.key.stable_id(), pr_number=pr.number,
                label_manager=self.label_manager, kind="integration_ruling_check",
                reason=(
                    f"Issue #{issue.number} carries standing ruling(s) {ids}. io never merges such a PR"
                    f" into `{self.branch}` itself: check the diff against each ruling, then merge it"
                    f" into `{self.branch}` yourself (a merge commit), or request changes."
                ),
            ))
        self._merge_candidates.append(MergeIntoIntegration(
            issue_number=issue.number, issue_key=issue.key.stable_id(), pr_number=pr.number,
            pr_url=pr.url, pr_title=pr.title, head_sha=head, integration_branch=self.branch,
            integration_tip=tip, merge_method=self.config.merge_method,
        ))
        return IntegrationRouting(_NOTHING)

    def discovered_steps(self) -> list[IntegrationStep]:
        """The pass's steps: every update and upkeep step, and ONE merge.

        Merges are serialized: the oldest ready PR (lowest number) merges this
        pass; the rest are re-checked against the new tip on the next.
        """
        merge = min(self._merge_candidates, key=lambda step: step.pr_number, default=None)
        return [*self.steps, *([merge] if merge is not None else [])]

    def _integration_tip(self) -> str | None:
        if not self._tip_read:
            self._tip = self.host.branch_head(self.branch)
            self._tip_read = True
        return self._tip

    # -- the branch and the delivery PR ------------------------------------------

    def upkeep(self, state: "OrchestratorState") -> None:
        """Keep the branch and the delivery PR (at most every upkeep interval)."""
        now = self.clock()
        if state.integration_upkeep_at and now - state.integration_upkeep_at < INTEGRATION_UPKEEP_INTERVAL_SECONDS:
            return
        state.integration_upkeep_at = now
        try:
            self._upkeep(state)
        except RepositoryHostError as error:
            # The branch keeping is not lifecycle-critical: the PRs' own steps
            # stand, and the next interval reads again.
            logger.warning("Integration: branch upkeep could not read GitHub: %s", error)

    def _upkeep(self, state: "OrchestratorState") -> None:
        default = self.host.get_default_branch()
        if default == self.branch:
            raise IntegrationConfigError(
                f"integration.branch {self.branch!r} is the repository's default branch;"
                " integration mode needs a separate branch"
            )
        default_tip = self.host.branch_head(default)
        if default_tip is None:
            raise RepositoryHostError(f"the default branch {default!r} has no head")
        tip = self._integration_tip()
        if tip is None:
            self.steps.append(CreateIntegrationBranch(branch=self.branch, from_sha=default_tip))
            state.integration_delivery = None
            return
        comparison = self.host.compare_commits(default, tip)
        if comparison.behind_by:
            if not comparison.ahead_by:
                # Delivered: integration continues from the default branch.
                self.steps.append(FastForwardIntegration(branch=self.branch, from_sha=tip, to_sha=default_tip))
                state.integration_delivery = None
                return
            self.steps.append(SyncIntegrationFromDefault(
                branch=self.branch, default_branch=default, integration_tip=tip, default_tip=default_tip,
            ))
        if not comparison.ahead_by:
            state.integration_delivery = None
            return
        self._keep_delivery_pr(state, default=default, tip=tip, comparison_shas=comparison.commit_shas,
                               ahead_by=comparison.ahead_by, complete=comparison.complete)

    def _keep_delivery_pr(
        self, state: "OrchestratorState", *, default: str, tip: str,
        comparison_shas: tuple[str, ...], ahead_by: int, complete: bool,
    ) -> None:
        delivery = self.host.find_open_pull_request(head=self.branch, base=default)
        known = state.integration_delivery
        if (
            delivery is not None and delivered_tip(delivery.body) == tip
            and known is not None and known.pr_number == delivery.number and known.integration_tip == tip
        ):
            return  # the body and the page are current
        merged = merged_in_delivery(self.host.merged_pull_requests_into(self.branch), comparison_shas)
        body = render_delivery_body(head=self.branch, base=default, tip=tip, ahead_by=ahead_by,
                                    merged=merged, complete=complete)
        if delivery is None:
            self.steps.append(OpenDeliveryPullRequest(
                head=self.branch, base=default, title=delivery_title(head=self.branch, base=default),
                body=body, integration_tip=tip,
            ))
            state.integration_delivery = None  # shown once the PR exists
            return
        if delivered_tip(delivery.body) != tip:
            self.steps.append(RefreshDeliveryPullRequest(pr_number=delivery.number, body=body, integration_tip=tip))
        state.integration_delivery = IntegrationDeliveryView(
            pr_number=delivery.number, url=delivery.url, head=self.branch, base=default,
            integration_tip=tip, ahead_by=ahead_by,
            merged_pr_numbers=tuple(pr.number for pr in merged),
            observed_at=datetime.fromtimestamp(self.clock(), tz=UTC).isoformat(),
        )


# -- the plan ------------------------------------------------------------------


def plan_integration_steps(steps: Sequence[IntegrationStep]) -> list["AdvanceIntegrationAction"]:
    """One action per discovered step; the owner already decided each."""
    from ..domain.integration_branch import step_issue_number, step_pr_number
    from .actions import AdvanceIntegrationAction

    return [
        AdvanceIntegrationAction(
            step=step, issue_number=step_issue_number(step) or 0, pr_number=step_pr_number(step) or 0,
            reason=describe_step(step),
        )
        for step in steps
    ]


# -- the writes ----------------------------------------------------------------


def apply_integration_step(
    action: "AdvanceIntegrationAction",
    *,
    host: "RepositoryHost",
    labels: "LabelManager",
    events: "EventSink",
    event_context: "EventContext | None" = None,
) -> "ActionResult":
    """Execute one integration step, re-checking what may have moved since discovery."""
    from .actions import ActionResult

    step = action.step
    assert step is not None, "an integration action carries its step"
    refusal = _refusal(step, host=host, labels=labels)
    if refusal is not None:
        _publish(events, event_context, EventName.INTEGRATION_STEP_SKIPPED, step, reason=refusal)
        return ActionResult.skip(action, refusal, pr_number=action.pr_number or None)
    try:
        detail = _write(step, host)
    except RepositoryHostError as error:
        logger.error("Integration step failed (%s): %s", describe_step(step), error)
        return ActionResult.fail_from(action, error, pr_number=action.pr_number or None)
    if detail is not None:
        # The write was refused by GitHub's own state (a conflicting sync):
        # nothing changed. The liveness owner bounds the repeats.
        _publish(events, event_context, EventName.INTEGRATION_STEP_SKIPPED, step, reason=detail)
        return ActionResult.fail(action, detail, pr_number=action.pr_number or None)
    logger.info("Integration: %s", describe_step(step))
    _publish(events, event_context, EventName.INTEGRATION_STEP_APPLIED, step)
    return ActionResult.ok(action, issue_number=action.issue_number or None, pr_number=action.pr_number or None)


def _refusal(step: IntegrationStep, *, host: "RepositoryHost", labels: "LabelManager") -> str | None:
    """Why *step* must not run now; None when it may."""
    if isinstance(step, MergeIntoIntegration):
        try:
            current = [host.get_issue_labels_fresh(n) for n in (step.issue_number, step.pr_number)]
        except RepositoryHostError as error:
            return f"labels of #{step.issue_number}/PR #{step.pr_number} unreadable before the merge: {error}"
        if holds_merge(labels, *current):
            return f"issue #{step.issue_number} or PR #{step.pr_number} now awaits a person; not merged"
        tip = host.branch_head(step.integration_branch)
        if tip != step.integration_tip:
            return (f"{step.integration_branch} moved ({step.integration_tip[:8]} -> {(tip or 'gone')[:8]})"
                    f" since PR #{step.pr_number} was checked against it; re-checked next pass")
    return None


def _write(step: IntegrationStep, host: "RepositoryHost") -> str | None:
    """Perform *step*; a non-None return is GitHub refusing it without an error."""
    if isinstance(step, MergeIntoIntegration):
        title, message = merge_commit_message(
            pr_number=step.pr_number, title=step.pr_title, branch=step.integration_branch,
            head_sha=step.head_sha, tip=step.integration_tip,
        )
        host.merge_pull_request(step.pr_number, head_sha=step.head_sha, method=step.merge_method,
                                title=title, message=message)
    elif isinstance(step, UpdatePullRequestBranch):
        host.update_pull_request_branch(step.pr_number, expected_head_sha=step.head_sha)
    elif isinstance(step, CreateIntegrationBranch):
        host.create_branch(step.branch, step.from_sha)
    elif isinstance(step, FastForwardIntegration):
        if host.branch_head(step.branch) != step.from_sha:
            return f"{step.branch} moved since it was compared; re-checked next pass"
        host.fast_forward_branch(step.branch, step.to_sha)
    elif isinstance(step, SyncIntegrationFromDefault):
        outcome = host.merge_branch(
            base=step.branch, head=step.default_tip,
            message=f"Merge {step.default_branch} into {step.branch} (issue-orchestrator, #8144)",
        )
        if outcome is BranchMergeOutcome.CONFLICT:
            return (f"{step.default_branch} ({step.default_tip[:8]}) conflicts with {step.branch}"
                    f" ({step.integration_tip[:8]}); a person must merge it")
    elif isinstance(step, OpenDeliveryPullRequest):
        host.create_pr(step.title, step.body, head=step.head, base=step.base)
    else:
        host.update_pull_request_body(step.pr_number, step.body)
    return None


def _publish(
    events: "EventSink", context: "EventContext | None", name: EventName, step: IntegrationStep, *, reason: str = "",
) -> None:
    payload: dict[str, object] = {"step": type(step).__name__, "summary": describe_step(step), "reason": reason}
    payload.update(
        (attribute, getattr(step, attribute))
        for attribute in ("issue_number", "pr_number", "head_sha", "integration_tip", "branch", "to_sha")
        if hasattr(step, attribute)
    )
    events.publish(make_trace_event(name, context.enrich(payload) if context is not None else payload))


__all__ = [
    "INTEGRATION_UPKEEP_INTERVAL_SECONDS",
    "IntegrationBranchOwner",
    "IntegrationConfigError",
    "IntegrationRouting",
    "apply_integration_step",
    "plan_integration_steps",
]
