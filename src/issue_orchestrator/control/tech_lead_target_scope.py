"""Which issues/PRs a tech-lead decision may TARGET, and how a violation reads.

Split out of ``tech_lead_completion`` because it is one self-contained rule with
its own vocabulary. The completion owner's job is to decide the session's
outcome — load the artifact pair, resolve the launch authority, plan the
effects; deciding whether ``A3`` may address ``#999`` is a separate question
answered entirely from the decision plus the immutable launch authority, and it
is the question that grows a case every time the action vocabulary does
(``defer_to_tracker`` is #6971's).

Two scopes, deliberately disjoint for a health review (#6764 re-review F1):

* **General** (:data:`TARGET_SCOPED_ACTION_TYPES`) — comment/routing proposals
  may address the general launch scope, which for a batch review includes the
  audited manifest PRs.
* **Act-level** (``ACT_LEVEL_TECH_LEAD_ACTIONS``) — reset/kill are held to the
  STRICTER issue-only scope, because their target is handed to the issue reset
  owner as an ``issue_number``: a manifest PR number, or a tech-lead bookkeeping
  anchor, is a confused deputy that resets the wrong entity.

``create_issue`` and ``flag_pattern`` carry no target and are scope-free by
construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS
from ..domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import TechLeadDecision

#: Comment/routing proposals whose ``target_number`` must fall inside the
#: general launch scope.
#: Tracker dispositions additionally require the failure-only immutable tracker
#: grant, enforced by investigation_disposition_violation in the disposition owner.
TARGET_SCOPED_ACTION_TYPES = frozenset(
    ("post_comment", "escalate_to_human", "defer_to_tracker")
)


def _launch_scope_description(
    authority: TechLeadLaunchAuthority, allowed: frozenset[int]
) -> str:
    """Human-readable launch scope for out-of-scope violation messages."""
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return f"the originating issue #{authority.focus_issue_number}"
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        return (
            f"the health-review anchor issue #{authority.anchor_issue_number}"
            " (board-wide comments/escalations belong on the anchor; act-level"
            " proposals instead use the cohort this review owns, published as"
            " problem_cohort in board-snapshot.json)"
        )
    return (
        "the audited manifest PRs and the tracking issue"
        f" ({', '.join(f'#{n}' for n in sorted(allowed))})"
    )


def _act_level_scope_description(authority: TechLeadLaunchAuthority) -> str:
    """Human-readable ISSUE-only scope for an out-of-scope act-level violation."""
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return f"the originating work issue #{authority.focus_issue_number}"
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        cohort = ", ".join(f"#{n}" for n in authority.problem_issue_numbers)
        return (
            "the health review's immutable problem cohort, published as"
            " problem_cohort in board-snapshot.json"
            f" ({cohort or 'empty — a periodic review owns no act-level target'})"
        )
    return (
        "no work issue is in scope for an act-level reset/kill from this"
        " session — that intent applies only to a failure investigation's"
        " focus issue; batch manifest entries are PRs and tech_lead anchors are"
        " bookkeeping issues, so route board findings through the scope-free"
        " create_issue/flag_pattern proposals instead"
    )


def target_scope_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """Out-of-scope target detail for any targeted proposal, or None.

    Two scopes (#6764 re-review F1): comment/routing proposals may target the
    general launch scope (manifest PRs included for a batch), while act-level
    reset/kill proposals are held to the STRICTER issue-only scope so a
    manifest PR number never reaches the issue reset owner as an ``issue_number``.
    """
    allowed = authority.allowed_targets()
    act_allowed = authority.allowed_act_level_targets()
    for action in decision.proposed_actions:
        if action.action_type in ACT_LEVEL_TECH_LEAD_ACTIONS:
            if action.target_number not in act_allowed:
                return (
                    f"proposed action {action.id} ({action.action_type}) targets"
                    f" #{action.target_number}, outside this session's launch"
                    f" scope for an act-level reset/kill:"
                    f" {_act_level_scope_description(authority)}"
                )
            continue
        if action.action_type not in TARGET_SCOPED_ACTION_TYPES:
            continue
        if action.target_number not in allowed:
            return (
                f"proposed action {action.id} ({action.action_type}) targets"
                f" #{action.target_number}, outside this session's launch"
                f" scope: {_launch_scope_description(authority, allowed)}"
            )
    return None
