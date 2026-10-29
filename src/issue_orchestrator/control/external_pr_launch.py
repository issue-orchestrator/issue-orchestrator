"""THE admission rule at the review and rework launch boundary (#9499).

A review or rework of a PR the integration effort governs (an adopted PR, or
any external PR into the effort branch -
:func:`~..domain.external_pr_adoption.effort_governs`) is queued under a
complete :class:`~..domain.external_pr_adoption.LaunchAuthority` (the effort,
and exactly the PR, issue and agent it was admitted for), persisted with the
queued item, then waits for a slot. Its issue may leave the effort or change its agent, its PR may lose its
origin label, retarget, or point at another issue meanwhile. So each launch
asks THE admission rule again - its FIRST stateful decision, before any
provider, prompt or worktree step - on fresh reads, for exactly the PR, issue
and agent it would act as (:func:`~..domain.external_pr_adoption.launch_refusal`):

* admitted -> the launch proceeds;
* no longer admitted -> the queued work is WITHDRAWN (dropped, not retried);
* unreadable -> a RETRYABLE_FAILURE, on the queue's bounded budget.

The work counts as governed when its queued authority OR the PR's current
facts say so: no later mix of edits erases that custody. With no effort
running here (integration mode off), work queued under an authority, or on a
PR an effort adopted, fails closed: it is withdrawn, never launched unowned.

Every queued review and rework passes ONE boundary, :class:`QueuedWorkAdmission`
(the queue handlers): an item its producer already admitted carries its
authority - the scans, the reconciler's reworks, a governed rework's
completion (the session's own authority) - and any other is admitted there,
freshly, before it is queued: a publish retry's or staged finalization's
review, whatever the producer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from ..domain.external_pr_adoption import (
    LAUNCH_ADMITTED,
    ORIGIN_EXTERNAL_LABEL,
    LaunchAdmission,
    LaunchRequest,
    LaunchVerdict,
    effort_governs,
    launch_authority,
    launch_refusal,
)
from ..infra.logging_config import issue_log
from ..ports.repository_host import RepositoryHostError
from ..domain.models import PendingReview, PendingRework
from ..domain.session_kind import SessionKind
from .external_pr_adoption import ExternalPullRequestGate, linked_issue_facts, routing_gate, write_gate
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


def pr_admission(gate: ExternalPullRequestGate, request: LaunchRequest) -> LaunchAdmission:
    """Whether io may review or rework *request*'s PR as its issue's, on fresh
    reads: always for work no effort governs (by its queued authority or the
    PR's current facts), otherwise only while THE admission rule admits it -
    the admission then carrying the authority such work is queued under."""
    authority = request.authority
    if not gate.config.integration.enabled:
        return _without_an_effort(gate, request)
    pr_number, issue_number = request.pr_number, request.issue_number
    try:
        pr = gate.host.get_pr(pr_number)
        if pr is None:
            # A PR found at launch that is not there is no PR the effort governs.
            return LAUNCH_ADMITTED if request.resolved_at_launch else _withdrawn(f"PR #{pr_number} is gone")
        pr_labels = gate.host.get_issue_labels_fresh(pr_number)
        facts = gate.facts(pr, pr_labels=pr_labels)
        if authority is None and not effort_governs(gate.branch, facts):
            return LAUNCH_ADMITTED
        issue = gate.host.get_issue(issue_number)
        if issue is None:
            return _withdrawn(f"issue #{issue_number} is gone")
        issue_facts = linked_issue_facts(gate.config, issue, labels=gate.host.get_issue_labels_fresh(issue_number))
        refused = launch_refusal(gate.branch, request, facts, issue=issue_facts, custody=gate.custody.custody(issue_number))
    except RepositoryHostError as error:
        return LaunchAdmission(LaunchVerdict.UNREADABLE, f"PR #{pr_number} could not be re-read: {error}")
    if refused is not None:
        return _withdrawn(refused)
    return LaunchAdmission(LaunchVerdict.ADMITTED, authority=launch_authority(gate.branch, facts, issue_facts))


def _without_an_effort(gate: ExternalPullRequestGate, request: LaunchRequest) -> LaunchAdmission:
    """No integration effort runs here: work queued under one, or on a PR one
    adopted, fails closed; any other work is no effort's business. One
    (conditional) PR read: the origin label is the adoption."""
    if request.authority is not None:
        return _withdrawn(f"queued under the `{request.authority.effort}` integration effort,"
                          " which this engine no longer runs")
    try:
        pr = gate.host.get_pr(request.pr_number)
    except RepositoryHostError as error:
        return LaunchAdmission(LaunchVerdict.UNREADABLE, f"PR #{request.pr_number} could not be re-read: {error}")
    adopted = pr is not None and ORIGIN_EXTERNAL_LABEL in pr.labels
    if adopted:
        return _withdrawn(f"PR #{request.pr_number} was adopted by an integration effort this engine no longer runs")
    return LAUNCH_ADMITTED


def _withdrawn(reason: str) -> LaunchAdmission:
    return LaunchAdmission(LaunchVerdict.WITHDRAWN, reason)


@dataclass(frozen=True)
class QueuedWorkAdmission:
    """THE queue boundary for review and rework of a PR (#9499).

    An item that carries an authority was admitted under it by its producer
    and is queued as it is (its launch re-judges it against that authority).
    Any other is admitted here on fresh reads: refused, it is not queued (the
    PR's labels bring it back to its lane's gated scan); admitted, it is
    queued under the authority the admission returns - with that authority's
    agent - or none when no effort governs it.
    """

    gate: Callable[[], ExternalPullRequestGate]

    def review(self, review: PendingReview) -> PendingReview | None:
        if review.authority is not None:
            return review
        admission = self._admit(LaunchRequest(
            SessionKind.REVIEW, review.issue_number, review.pr_number, None, review.agent_label))
        if admission is None:
            return None
        authority = admission.authority
        return review if authority is None else replace(review, agent_label=authority.agent, authority=authority)

    def rework(self, rework: PendingRework) -> PendingRework | None:
        issue_number = rework.resolve_issue_number()
        if rework.authority is not None or not rework.pr_number or issue_number is None:
            return rework  # a PR-less rework is admitted at launch, on the PR it finds then
        admission = self._admit(LaunchRequest(
            SessionKind.REWORK, issue_number, rework.pr_number, None, rework.agent_type))
        if admission is None:
            return None
        authority = admission.authority
        return rework if authority is None else replace(rework, agent_type=authority.agent, authority=authority)

    def _admit(self, request: LaunchRequest) -> LaunchAdmission | None:
        admission = pr_admission(self.gate(), request)
        if admission.verdict is LaunchVerdict.ADMITTED:
            return admission
        logger.info(issue_log(request.issue_number, "[queue] %s of PR #%d not queued (%s): %s"),
                    request.kind.value, request.pr_number, admission.verdict.value, admission.reason)
        return None


def queued_work_admission(
    config: "Config", host: "RepositoryHost", state: Callable[[], "OrchestratorState"],
) -> QueuedWorkAdmission:
    """The queue boundary's owner, on the routing's view of live state."""
    return QueuedWorkAdmission(lambda: routing_gate(config, host, state()))


class ExternalLaunchAdmission(Protocol):
    """The launch boundary's dependency: refuse a launch the effort no longer admits."""

    def refuse(self, request: LaunchRequest) -> LaunchResult | None: ...


@dataclass(frozen=True)
class ExternalLaunchGate:
    """:func:`pr_admission` on a write's fresh gate, as a launch result."""

    gate: Callable[[], ExternalPullRequestGate]

    def refuse(self, request: LaunchRequest) -> LaunchResult | None:
        admission = pr_admission(self.gate(), request)
        if admission.verdict is LaunchVerdict.ADMITTED:
            return None
        logger.info(issue_log(request.issue_number, "[launch] %s of PR #%d not launched (%s): %s"),
                    request.kind.value, request.pr_number, admission.verdict.value, admission.reason)
        disposition = (LaunchDisposition.WITHDRAWN if admission.verdict is LaunchVerdict.WITHDRAWN
                       else LaunchDisposition.RETRYABLE_FAILURE)
        return LaunchResult(None, False, f"Not admitted by the integration effort: {admission.reason}",
                            disposition=disposition)


@dataclass(frozen=True)
class NoIntegrationEffort:
    """A launcher composed without the integration effort: nothing to refuse."""

    def refuse(self, request: LaunchRequest) -> LaunchResult | None:
        del request
        return None


NO_INTEGRATION_EFFORT = NoIntegrationEffort()


def launch_gate(
    config: "Config", host: "RepositoryHost", state: Callable[[], "OrchestratorState"],
) -> ExternalLaunchAdmission:
    """The launch boundary's gate: a write's fresh reads, bound to live state."""
    return ExternalLaunchGate(lambda: write_gate(config, host, state))


__all__ = [
    "ExternalLaunchAdmission",
    "ExternalLaunchGate",
    "LaunchRequest",
    "NO_INTEGRATION_EFFORT",
    "NoIntegrationEffort",
    "QueuedWorkAdmission",
    "launch_gate",
    "pr_admission",
    "queued_work_admission",
]
