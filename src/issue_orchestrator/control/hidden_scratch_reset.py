"""Hidden issue scratch-reset preflight decisions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HiddenScratchResetDecision:
    """Preflight result for one hidden issue scratch-reset request."""

    issue: int
    title: str | None
    state: str | None
    labels: list[str]
    eligible: bool
    action: str
    will_reopen: bool
    reason: str

    def to_payload(self) -> dict[str, Any]:
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


def preflight_hidden_scratch_reset_issues(
    *,
    issue_numbers: list[int],
    repository_host: Any,
    config: Any,
) -> list[HiddenScratchResetDecision]:
    decisions: list[HiddenScratchResetDecision] = []
    for number in issue_numbers:
        try:
            issue = repository_host.get_issue(number)
        except Exception as exc:
            logger.error(
                "[reset-retry] Failed hidden scratch preflight for issue #%d: %s",
                number,
                exc,
                exc_info=True,
            )
            decisions.append(_skip(number, f"Unable to fetch issue: {exc}"))
            continue
        decisions.append(preflight_hidden_scratch_reset_issue(number, issue, config))
    return decisions


def preflight_hidden_scratch_reset_issue(
    number: int,
    issue: Any | None,
    config: Any,
) -> HiddenScratchResetDecision:
    if issue is None:
        return _skip(number, f"Issue #{number} not found")

    marks = list(getattr(issue, "labels", []) or [])
    current = str(getattr(issue, "state", "") or "open").lower()
    title = getattr(issue, "title", None)
    scope_issue = _copy_with_state(issue, "open") if current == "closed" else issue
    scope_detail = _scope_skip_detail(config, scope_issue)
    if scope_detail is not None:
        return HiddenScratchResetDecision(
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
        return HiddenScratchResetDecision(
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
        return HiddenScratchResetDecision(
            issue=number,
            title=title,
            state=current,
            labels=marks,
            eligible=True,
            action="reopen_and_reset",
            will_reopen=True,
            reason="Closed issue will be reopened before reset from scratch",
        )
    return HiddenScratchResetDecision(
        issue=number,
        title=title,
        state=current,
        labels=marks,
        eligible=True,
        action="reset",
        will_reopen=False,
        reason="Issue will be reset from scratch",
    )


def hidden_scratch_preflight_payload(
    decisions: list[HiddenScratchResetDecision],
) -> dict[str, Any]:
    eligible = [decision.issue for decision in decisions if decision.eligible]
    skipped = [decision.issue for decision in decisions if not decision.eligible]
    will_reopen = [decision.issue for decision in decisions if decision.will_reopen]
    return {
        "decisions": [decision.to_payload() for decision in decisions],
        "eligible": eligible,
        "skipped": skipped,
        "will_reopen": will_reopen,
        "from_scratch": True,
    }


def _copy_with_state(issue: Any, new_value: str) -> Any:
    try:
        return replace(issue, state=new_value)
    except TypeError:
        return SimpleNamespace(
            number=getattr(issue, "number"),
            title=getattr(issue, "title", None),
            labels=list(getattr(issue, "labels", []) or []),
            state=new_value,
            milestone=getattr(issue, "milestone", None),
            agent_type=getattr(issue, "agent_type", None),
        )


def _scope_skip_detail(config: Any, issue: Any) -> str | None:
    marks = list(getattr(issue, "labels", []) or [])
    scope_key = getattr(config.filtering, "label", None)
    if scope_key and scope_key not in marks:
        return f'missing required filter label "{scope_key}"'

    milestones = config.get_filter_milestones()
    current_milestone = getattr(issue, "milestone", None)
    if milestones and current_milestone not in milestones:
        current = current_milestone or "none"
        return f'milestone "{current}" is not one of {", ".join(milestones)}'

    excluded = [
        mark for mark in marks
        if mark in set(getattr(config.filtering, "exclude_labels", []) or [])
    ]
    if excluded:
        return f'has excluded label "{excluded[0]}"'

    prefixes = tuple(getattr(config.filtering, "exclude_label_prefixes", []) or ())
    for mark in marks:
        for prefix in prefixes:
            if mark.startswith(prefix):
                return f'has label "{mark}" matching excluded prefix "{prefix}"'

    if not config.get_issue_filter().apply([issue]):
        return "excluded by configured issue label filter"

    target = getattr(config.filtering, "issue", None)
    if target and issue.number != target:
        return f"engine is scoped to issue #{target}"

    return None


def _agent_skip_detail(config: Any, issue: Any) -> str | None:
    agent_marker = getattr(issue, "agent_type", None)
    if not agent_marker:
        return f"Issue #{issue.number} has no agent:* label"
    if agent_marker not in config.agents:
        return f'Issue #{issue.number} uses unconfigured agent label "{agent_marker}"'
    return None


def _skip(number: int, reason: str) -> HiddenScratchResetDecision:
    return HiddenScratchResetDecision(
        issue=number,
        title=None,
        state=None,
        labels=[],
        eligible=False,
        action="skipped",
        will_reopen=False,
        reason=reason,
    )
