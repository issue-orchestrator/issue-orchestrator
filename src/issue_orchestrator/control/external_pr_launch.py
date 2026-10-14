"""THE admission rule at the review and rework launch boundary (#9499).

A review or rework of a PR the integration effort governs (an adopted PR, or
any external PR into the effort branch -
:func:`~..domain.external_pr_adoption.effort_governs`) is queued while the PR
is admitted, then waits for a slot. Its issue may leave the effort, change its
agent, its PR may lose its origin label, retarget, or point at another issue
meanwhile. So each launch re-asks THE admission rule on fresh reads, just
before its effects, for exactly the PR and issue it would act on:

* admitted -> the launch proceeds;
* no longer admitted -> the queued work is WITHDRAWN (dropped, not retried);
* unreadable -> a RETRYABLE_FAILURE, on the queue's bounded budget.

Discovery asks the same question (:func:`pr_admission`) before it queues a
review or a rework, so a PR the rule refuses is not queued every scan only to
be withdrawn at launch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.external_pr_adoption import LAUNCH_ADMITTED, LaunchAdmission, LaunchVerdict, effort_governs
from ..domain.session_kind import SessionKind
from ..infra.logging_config import issue_log
from ..ports.repository_host import RepositoryHostError
from .external_pr_adoption import ExternalPullRequestGate, write_gate
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


def pr_admission(gate: ExternalPullRequestGate, *, issue_number: int, pr_number: int) -> LaunchAdmission:
    """Whether io may review or rework PR *pr_number* as issue *issue_number*'s,
    on fresh reads: always for a PR the effort does not govern, otherwise only
    while THE admission rule admits it for exactly that issue."""
    if not gate.config.integration.enabled:
        return LAUNCH_ADMITTED
    try:
        pr = gate.host.get_pr(pr_number)
        if pr is None:
            return LaunchAdmission(LaunchVerdict.WITHDRAWN, f"PR #{pr_number} is gone")
        pr_labels = gate.host.get_issue_labels_fresh(pr_number)
        facts = gate.facts(pr, pr_labels=pr_labels)
        if not effort_governs(gate.branch, facts):
            return LAUNCH_ADMITTED
        issue = gate.host.get_issue(issue_number)
        if issue is None:
            return LaunchAdmission(LaunchVerdict.WITHDRAWN, f"issue #{issue_number} is gone")
        refused = gate.refusal(
            pr=pr, issue=issue, issue_labels=gate.host.get_issue_labels_fresh(issue_number), pr_labels=pr_labels,
        )
    except RepositoryHostError as error:
        return LaunchAdmission(LaunchVerdict.UNREADABLE, f"PR #{pr_number} could not be re-read: {error}")
    return LAUNCH_ADMITTED if refused is None else LaunchAdmission(LaunchVerdict.WITHDRAWN, refused)


class ExternalLaunchAdmission(Protocol):
    """The launch boundary's dependency: refuse a launch the effort no longer admits."""

    def refuse(self, *, kind: SessionKind, issue_number: int, pr_number: int) -> LaunchResult | None: ...


@dataclass(frozen=True)
class ExternalLaunchGate:
    """:func:`pr_admission` on a write's fresh gate, as a launch result."""

    gate: Callable[[], ExternalPullRequestGate]

    def refuse(self, *, kind: SessionKind, issue_number: int, pr_number: int) -> LaunchResult | None:
        admission = pr_admission(self.gate(), issue_number=issue_number, pr_number=pr_number)
        if admission.verdict is LaunchVerdict.ADMITTED:
            return None
        logger.info(issue_log(issue_number, "[launch] %s of PR #%d not launched (%s): %s"),
                    kind.value, pr_number, admission.verdict.value, admission.reason)
        disposition = (LaunchDisposition.WITHDRAWN if admission.verdict is LaunchVerdict.WITHDRAWN
                       else LaunchDisposition.RETRYABLE_FAILURE)
        return LaunchResult(None, False, f"Not admitted by the integration effort: {admission.reason}",
                            disposition=disposition)


@dataclass(frozen=True)
class NoIntegrationEffort:
    """A launcher composed without the integration effort: nothing to refuse."""

    def refuse(self, *, kind: SessionKind, issue_number: int, pr_number: int) -> LaunchResult | None:
        del kind, issue_number, pr_number
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
    "NO_INTEGRATION_EFFORT",
    "NoIntegrationEffort",
    "launch_gate",
    "pr_admission",
]
