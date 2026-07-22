"""Milestone intent value object for orchestrator-created tech_lead issues.

A pure, frozen domain value object (dataclasses-only dependencies). It lives in
the domain layer rather than ``control/actions.py`` because it carries no
plan/apply semantics — it is the typed milestone intent the create-issue
boundary resolves. ``control.actions`` re-exports it so existing importers are
unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TechLeadMilestoneIntent:
    """Configured milestone intent for an orchestrator-created tech_lead issue.

    Carried on :class:`~..control.actions.CreateTechLeadIssueAction` so the
    explicit-strategy name -> number resolution happens ONCE, at the
    create-issue execution boundary
    (``action_applier._apply_create_tech_lead_issue``), never at planning or
    completion time (#6769 finding 4): a shadow-mode ``create_issue`` proposal
    plans zero GitHub reads, and an unresolvable configured name fails the
    creation loudly instead of the completion.

    Exactly one shape at a time:
    - ``explicit_name`` — ``tech_lead.milestone_strategy.explicit``; the applier
      resolves it against the repository's milestones and fails loudly when
      it matches none.
    - ``inherited_number`` — a number already known at planning time
      (``inherit_from_issues``); no API read needed.
    - neither — no milestone.
    """

    explicit_name: str | None = None
    inherited_number: int | None = None

    def __post_init__(self) -> None:
        if self.explicit_name is not None and self.inherited_number is not None:
            raise ValueError(
                "TechLeadMilestoneIntent carries a name OR a number, never both"
            )
