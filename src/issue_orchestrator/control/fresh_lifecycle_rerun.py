"""Fresh lifecycle rerun preflight decisions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.fresh_lifecycle_rerun import FRESH_LIFECYCLE_RERUN_INTENT
from .issue_scope import issue_scope_skip_detail

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreshLifecycleRerunDecision:
    """Preflight result for one fresh lifecycle rerun request."""

    issue: int
    title: str | None
    state: str | None
    labels: list[str]
    eligible: bool
    action: str
    will_reopen: bool
    reason: str

    def to_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "title": self.title,
            "state": self.state,
            "labels": self.labels,
            "eligible": self.eligible,
            "action": self.action,
            "will_reopen": self.will_reopen,
            "reason": self.reason,
        }


def preflight_fresh_lifecycle_rerun_issues(
    *,
    issue_numbers: list[int],
    repository_host: "RepositoryHost",
    config: "Config",
) -> list[FreshLifecycleRerunDecision]:
    decisions: list[FreshLifecycleRerunDecision] = []
    for number in issue_numbers:
        try:
            issue = repository_host.get_issue(number)
        except Exception as exc:
            logger.error(
                "[reset-retry] Failed fresh lifecycle rerun preflight for issue #%d: %s",
                number,
                exc,
                exc_info=True,
            )
            decisions.append(_skip(number, f"Unable to fetch issue: {exc}"))
            continue
        decisions.append(preflight_fresh_lifecycle_rerun_issue(number, issue, config))
    return decisions


def preflight_fresh_lifecycle_rerun_issue(
    number: int,
    issue: "Issue | None",
    config: "Config",
) -> FreshLifecycleRerunDecision:
    if issue is None:
        return _skip(number, f"Issue #{number} not found")

    marks = list(issue.labels)
    current = str(issue.state or "open").lower()
    title = issue.title
    scope_detail = issue_scope_skip_detail(
        config,
        issue,
        require_open=False,
        include_issue_number_filter=True,
    )
    if scope_detail is not None:
        return FreshLifecycleRerunDecision(
            issue=number,
            title=title,
            state=current,
            labels=marks,
            eligible=False,
            action="skipped",
            will_reopen=False,
            reason=(
                f"Issue #{number} is outside this engine's scope after reopen: "
                f"{scope_detail}"
                if current == "closed"
                else f"Issue #{number} is outside this engine's scope: {scope_detail}"
            ),
        )

    agent_detail = _agent_skip_detail(config, issue)
    if agent_detail is not None:
        return FreshLifecycleRerunDecision(
            issue=number,
            title=title,
            state=current,
            labels=marks,
            eligible=False,
            action="skipped",
            will_reopen=False,
            reason=agent_detail,
        )

    if current == "closed":
        return FreshLifecycleRerunDecision(
            issue=number,
            title=title,
            state=current,
            labels=marks,
            eligible=True,
            action="reopen_and_reset",
            will_reopen=True,
            reason="Closed issue will be reopened before reset from scratch",
        )
    return FreshLifecycleRerunDecision(
        issue=number,
        title=title,
        state=current,
        labels=marks,
        eligible=True,
        action="reset",
        will_reopen=False,
        reason="Issue will be reset from scratch",
    )


def fresh_lifecycle_rerun_preflight_payload(
    decisions: list[FreshLifecycleRerunDecision],
) -> dict[str, object]:
    eligible = [decision.issue for decision in decisions if decision.eligible]
    skipped = [decision.issue for decision in decisions if not decision.eligible]
    will_reopen = [decision.issue for decision in decisions if decision.will_reopen]
    return {
        "decisions": [decision.to_payload() for decision in decisions],
        "eligible": eligible,
        "skipped": skipped,
        "will_reopen": will_reopen,
        "from_scratch": True,
        "rerun_intent": FRESH_LIFECYCLE_RERUN_INTENT,
    }


def _agent_skip_detail(config: "Config", issue: "Issue") -> str | None:
    agent_marker = issue.agent_type
    if not agent_marker:
        return f"Issue #{issue.number} has no agent:* label"
    if agent_marker not in config.agents:
        return f'Issue #{issue.number} uses unconfigured agent label "{agent_marker}"'
    return None


def _skip(number: int, reason: str) -> FreshLifecycleRerunDecision:
    return FreshLifecycleRerunDecision(
        issue=number,
        title=None,
        state=None,
        labels=[],
        eligible=False,
        action="skipped",
        will_reopen=False,
        reason=reason,
    )
