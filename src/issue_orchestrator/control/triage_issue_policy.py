"""Single policy owner for orchestrator-created triage issues (ADR-0031).

Two paths create GitHub issues on triage's behalf and MUST share one policy:

* the planner's batch-review tracking issue (``_plan_triage_issue_creation``),
* decision-driven follow-up issues (``create_issue`` proposals in a triage
  decision artifact, executed by ``triage_decision_actions``).

This module owns how the ``triage:`` config section (explicit labels,
inherited labels, priority, milestone strategy) turns into concrete issue
labels/milestones/titles, and which agent-proposed labels are acceptable at
all. Agent-proposed labels are untrusted input: anything matching a
workflow/protected family (orchestrator lifecycle labels, ``needs-*``,
``*-reviewed``, ``*-failed``, ``publish-*``, ``blocked*``, ``agent:*``,
``triage:*``) is rejected so a decision artifact can never corrupt label
truth (ADR-0013). Concrete orchestrator-owned names are derived from
config/:class:`LabelManager`, not re-hardcoded here.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING

from .label_manager import LabelManager

if TYPE_CHECKING:
    from ..infra.config import Config


# Workflow label families that no agent-proposed label may match. These are
# families, not concrete names: concrete orchestrator-owned names (including
# any configured prefix) come from LabelManager/config at call time.
_PROTECTED_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"needs-", re.IGNORECASE),
    re.compile(r".*-reviewed\Z", re.IGNORECASE),
    re.compile(r".*-failed\Z", re.IGNORECASE),
    re.compile(r"publish-", re.IGNORECASE),
    re.compile(r"blocked", re.IGNORECASE),
    re.compile(r"agent:", re.IGNORECASE),
    re.compile(r"triage:", re.IGNORECASE),
)


def is_protected_triage_label(
    label: str, *, config: "Config", labels: LabelManager
) -> bool:
    """True when an agent-proposed label would touch workflow label truth."""
    if labels.is_ours(label) or labels.is_blocking(label):
        return True
    configured = {
        value
        for value in (
            config.triage_review_agent,
            config.triage_watch_label,
            config.triage_reviewed_label,
            config.triage_failed_label,
            config.filtering.label,
            config.label_in_progress,
        )
        if value
    }
    if label in configured:
        return True
    return any(pattern.match(label) for pattern in _PROTECTED_LABEL_PATTERNS)


def protected_triage_label_violations(
    proposed: Iterable[str], *, config: "Config", labels: LabelManager
) -> list[str]:
    """Return the agent-proposed labels that violate the protected set."""
    return [
        label
        for label in proposed
        if is_protected_triage_label(label, config=config, labels=labels)
    ]


def apply_triage_priority_prefix(config: "Config", title: str) -> str:
    """Apply the configured ``triage.priority`` tier as a ``[P?-000]`` prefix."""
    priority = (config.triage.priority or "").strip()
    if not re.fullmatch(r"P\d", priority):
        return title
    if re.search(r"^\[P\d-\d+\]", title):
        return title
    return f"[{priority}-000] {title}"


def triage_issue_milestone(
    config: "Config", source_milestones: Sequence[tuple[int, str]]
) -> int | None:
    """Compute the milestone for a triage-created issue per config strategy."""
    strategy = config.triage.milestone_strategy
    if strategy.explicit:
        # Explicit milestone is configured by NAME; number lookup is not
        # implemented, so the strategy intentionally yields no milestone
        # rather than guessing (mirrors the pre-extraction planner note).
        return None
    if strategy.inherit_from_issues and source_milestones:
        ordered = sorted(source_milestones, key=lambda m: m[0])
        if strategy.inherit_from_issues == "earliest":
            return ordered[0][0]
        return ordered[-1][0]
    return None


def batch_review_issue_labels(
    config: "Config", *, source_labels: Collection[str]
) -> tuple[str, ...]:
    """Labels for the planner's batch-review tracking issue."""
    base: list[str] = []
    if config.triage_review_agent:
        base.append(config.triage_review_agent)
    if config.filtering.label:
        base.append(config.filtering.label)
    return _with_configured_labels(config, base, source_labels=source_labels)


def decision_issue_labels(
    config: "Config",
    *,
    anchor_labels: Collection[str],
    agent_labels: Iterable[str],
    labels: LabelManager,
) -> tuple[str, ...]:
    """Labels for a decision-driven follow-up issue.

    Config policy first (filtering scope label, explicit labels, labels
    inherited from the triage session's anchor issue), then the agent's
    proposed labels. Protected agent labels are a bug at this point — the
    decision must have been rejected as a contract violation upstream
    (``triage_completion``) — so fail loudly instead of silently filtering.
    """
    violations = protected_triage_label_violations(
        agent_labels, config=config, labels=labels
    )
    if violations:
        raise ValueError(
            "protected labels must be rejected at decision validation, got: "
            + ", ".join(violations)
        )
    base: list[str] = []
    if config.filtering.label:
        base.append(config.filtering.label)
    composed = _with_configured_labels(config, base, source_labels=anchor_labels)
    return _deduped((*composed, *agent_labels))


def _with_configured_labels(
    config: "Config", base: list[str], *, source_labels: Collection[str]
) -> tuple[str, ...]:
    composed = list(base)
    composed.extend(config.triage.explicit_labels)
    composed.extend(
        label for label in config.triage.inherit_labels if label in source_labels
    )
    return _deduped(composed)


def _deduped(labels: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            result.append(label)
    return tuple(result)
