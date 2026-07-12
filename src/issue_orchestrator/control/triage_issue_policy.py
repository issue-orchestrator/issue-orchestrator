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

GitHub label names are case-insensitive, so every comparison in this module
casefolds — an agent must not bypass protection (or defeat inheritance or
dedup) by case-flipping a name.

The module is pure policy: the milestone strategy becomes a typed
:class:`TriageMilestoneIntent` at planning time (:func:`triage_issue_milestone_intent`),
and the explicit NAME -> number resolution runs ONCE at the create-issue
execution boundary (:func:`resolve_triage_milestone_number`, called by the
action applier with ``RepositoryHost.list_milestones`` passed in) — never at
planning or completion time (#6769 finding 4).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Mapping

from ..domain.triage_session import HEALTH_REVIEW_MARKER_LABEL
from .actions import TriageMilestoneIntent
from .label_manager import LabelManager

if TYPE_CHECKING:
    from ..infra.config import Config


# Workflow label families that no agent-proposed label may match. These are
# families, not concrete names: concrete orchestrator-owned names (including
# any configured prefix) come from LabelManager/config at call time.
# ``proposed-triage`` (#6778) is doubly covered: it is a registered
# LabelManager label (workflow-reserved) AND matched here, so the gate can
# only ever be orchestrator-attached — an agent proposing it is a contract
# violation regardless of which owner checks first.
_PROTECTED_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"needs-", re.IGNORECASE),
    re.compile(r".*-reviewed\Z", re.IGNORECASE),
    re.compile(r".*-failed\Z", re.IGNORECASE),
    re.compile(r"publish-", re.IGNORECASE),
    re.compile(r"blocked", re.IGNORECASE),
    re.compile(r"agent:", re.IGNORECASE),
    re.compile(r"triage:", re.IGNORECASE),
    re.compile(r"proposed-triage\Z", re.IGNORECASE),
)


def is_protected_triage_label(
    label: str, *, config: "Config", labels: LabelManager
) -> bool:
    """True when an agent-proposed label would touch workflow label truth.

    Case-insensitive throughout: GitHub treats ``WIP`` and ``wip`` as the
    same label.
    """
    if labels.is_workflow_reserved(label):
        return True
    folded = label.casefold()
    configured = {
        value.casefold()
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
    if folded in configured:
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


def resolve_triage_milestone_number(
    intent: TriageMilestoneIntent,
    list_milestones: Callable[[str], Sequence[Mapping[str, Any]]],
) -> int | None:
    """Resolve a milestone intent to a concrete number at the execution boundary.

    ``list_milestones`` is ``RepositoryHost.list_milestones`` passed in by the
    action applier so this module stays port-free; it is consulted only for
    an explicit name (one call, made only when an issue is actually being
    created — GitHub API discipline). Raises ValueError when the configured
    name matches no repository milestone — a misconfigured strategy must fail
    the creation loudly, never silently create unmilestoned issues.
    """
    if intent.explicit_name is None:
        return intent.inherited_number
    for milestone in list_milestones("all"):
        if str(milestone.get("title", "")).strip() == intent.explicit_name:
            return int(milestone["number"])
    raise ValueError(
        f"triage.milestone_strategy.explicit={intent.explicit_name!r} does not"
        " match any repository milestone; fix the configured name or remove"
        " the strategy"
    )


def triage_issue_milestone_intent(
    config: "Config",
    source_milestones: Sequence[tuple[int, str]],
) -> TriageMilestoneIntent:
    """Compute the milestone INTENT for a triage-created issue per config.

    Pure planning policy: the explicit strategy yields a name for the
    applier to resolve at creation time (#6769 finding 4); the inherit
    strategy yields a number already known from the source issues; otherwise
    no milestone.
    """
    strategy = config.triage.milestone_strategy
    name = (strategy.explicit or "").strip()
    if name:
        return TriageMilestoneIntent(explicit_name=name)
    if strategy.inherit_from_issues and source_milestones:
        ordered = sorted(source_milestones, key=lambda m: m[0])
        chosen = ordered[0] if strategy.inherit_from_issues == "earliest" else ordered[-1]
        return TriageMilestoneIntent(inherited_number=chosen[0])
    return TriageMilestoneIntent()


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


def health_review_issue_labels(config: "Config") -> tuple[str, ...]:
    """Labels for the periodic health-review anchor issue (ADR-0031 §4).

    Same configured policy batch anchors get — agent label, filtering scope
    label, ``triage.explicit_labels`` — plus the health marker label, which
    is crash-safe truth: the launcher derives the HEALTH_REVIEW flavor from
    it and the fact gatherer deduplicates open anchors by it. Health anchors
    have no source PRs, so ``triage.inherit_labels`` has nothing to inherit.
    """
    base = [
        value
        for value in (
            config.triage_review_agent,
            config.filtering.label,
            HEALTH_REVIEW_MARKER_LABEL,
        )
        if value
    ]
    return _with_configured_labels(config, base, source_labels=())


def triage_follow_up_agent_label(config: "Config") -> str:
    """The orchestrator-owned worker agent a ``create_issue`` proposal routes to.

    A triage decision may propose a NEW issue, but agent-proposed ``agent:*``
    labels are rejected as protected input (they could hijack routing), and
    ``explicit_labels`` defaults empty — so the created issue would carry no
    agent label and normal discovery (which queries per configured worker
    agent) would never fetch it. The orchestrator therefore assigns the
    destination itself: the first configured worker agent. Fails loudly when
    none is configured — a triage-enabled repo must have a worker agent
    (#6779 R5).
    """
    for agent_label in config.agents:
        return agent_label
    raise ValueError(
        "a triage create_issue proposal needs a destination worker agent, but"
        " config.agents is empty"
    )


def decision_issue_labels(
    config: "Config",
    *,
    anchor_labels: Collection[str],
    agent_labels: Iterable[str],
    labels: LabelManager,
    destination_agent: str,
    gate: bool = False,
) -> tuple[str, ...]:
    """Labels for a decision-driven follow-up issue.

    Config policy first (filtering scope label, explicit labels, labels
    inherited from the triage session's anchor issue), then the agent's
    proposed labels. Protected agent labels are a bug at this point — the
    decision must have been rejected as a contract violation upstream
    (``triage_completion``) — so fail loudly instead of silently filtering.

    ``destination_agent`` is the orchestrator-owned worker agent label the
    created issue is routed to (#6779 R5): appended AFTER the protection check
    (exempt like the gate — it is orchestrator-attached, not agent-proposed)
    so that removing the gate alone lands a fully schedulable issue. It must
    be a configured worker agent, else the issue would still be unschedulable.

    ``gate=True`` (propose-authority ``create_issue``, #6778) appends the
    orchestrator-attached ``proposed-triage`` gate AFTER the protection
    check: the gate is exempt from the agent-label allowlist here and ONLY
    here — an agent proposing it is still a contract violation.
    """
    violations = protected_triage_label_violations(
        agent_labels, config=config, labels=labels
    )
    if violations:
        raise ValueError(
            "protected labels must be rejected at decision validation, got: "
            + ", ".join(violations)
        )
    if destination_agent not in config.agents:
        raise ValueError(
            "decision_issue_labels destination_agent must be a configured worker"
            f" agent, got {destination_agent!r} (agents:"
            f" {sorted(config.agents)})"
        )
    base: list[str] = []
    if config.filtering.label:
        base.append(config.filtering.label)
    composed = _with_configured_labels(config, base, source_labels=anchor_labels)
    gate_labels = (labels.proposed_triage,) if gate else ()
    return _deduped((*composed, *agent_labels, destination_agent, *gate_labels))


def _with_configured_labels(
    config: "Config", base: list[str], *, source_labels: Collection[str]
) -> tuple[str, ...]:
    composed = list(base)
    composed.extend(config.triage.explicit_labels)
    source_folded = {label.casefold() for label in source_labels}
    composed.extend(
        label
        for label in config.triage.inherit_labels
        if label.casefold() in source_folded
    )
    return _deduped(composed)


def _deduped(labels: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving, case-insensitive dedup (first spelling wins)."""
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        folded = label.casefold()
        if label and folded not in seen:
            seen.add(folded)
            result.append(label)
    return tuple(result)
