"""THE admission rule at the review and rework launch boundary (#9499).

A review or rework of a PR the integration effort governs (an adopted PR, or
any external PR into the effort branch -
:func:`~..domain.external_pr_adoption.effort_governs`) is queued under that
effort's authority (``governing_effort`` on the queued item), then waits for a
slot. Its issue may leave the effort or change its agent, its PR may lose its
origin label, retarget, or point at another issue meanwhile. So each launch
asks THE admission rule again - its FIRST stateful decision, before any
provider, prompt or worktree step - on fresh reads, for exactly the PR, issue
and agent it would act as (:func:`~..domain.external_pr_adoption.launch_refusal`):

* admitted -> the launch proceeds;
* no longer admitted -> the queued work is WITHDRAWN (dropped, not retried);
* unreadable -> a RETRYABLE_FAILURE, on the queue's bounded budget.

The work counts as governed when its queued authority OR the PR's current
facts say so: no later mix of edits erases that custody.

Discovery asks the same question (:func:`pr_admission`) before it queues a
review or a rework, and records the governing effort it finds on the fact the
queued item is built from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.external_pr_adoption import (
    LAUNCH_ADMITTED,
    LaunchAdmission,
    LaunchRequest,
    LaunchVerdict,
    effort_governs,
)
from ..infra.logging_config import issue_log
from ..ports.repository_host import RepositoryHostError
from .external_pr_adoption import ExternalPullRequestGate, write_gate
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


def pr_admission(gate: ExternalPullRequestGate, request: LaunchRequest) -> LaunchAdmission:
    """Whether io may review or rework *request*'s PR as its issue's, on fresh
    reads: always for work no effort governs (by its queued authority or the
    PR's current facts), otherwise only while THE admission rule admits it."""
    if not gate.config.integration.enabled:
        return LAUNCH_ADMITTED
    pr_number, issue_number = request.pr_number, request.issue_number
    try:
        pr = gate.host.get_pr(pr_number)
        if pr is None:
            return LaunchAdmission(LaunchVerdict.WITHDRAWN, f"PR #{pr_number} is gone")
        pr_labels = gate.host.get_issue_labels_fresh(pr_number)
        facts = gate.facts(pr, pr_labels=pr_labels)
        governed = request.governing_effort is not None or effort_governs(gate.branch, facts)
        if not governed:
            return LAUNCH_ADMITTED
        issue = gate.host.get_issue(issue_number)
        if issue is None:
            return LaunchAdmission(LaunchVerdict.WITHDRAWN, f"issue #{issue_number} is gone", gate.branch)
        refused = gate.launch_refusal(
            request, pr=pr, issue=issue, issue_labels=gate.host.get_issue_labels_fresh(issue_number),
            pr_labels=pr_labels,
        )
    except RepositoryHostError as error:
        return LaunchAdmission(LaunchVerdict.UNREADABLE, f"PR #{pr_number} could not be re-read: {error}")
    if refused is None:
        return LaunchAdmission(LaunchVerdict.ADMITTED, governing_effort=gate.branch)
    return LaunchAdmission(LaunchVerdict.WITHDRAWN, refused, gate.branch)


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
    "launch_gate",
    "pr_admission",
]
