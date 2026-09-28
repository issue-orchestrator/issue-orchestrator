"""Rebuild a live tech-lead run's launch grant from durable truth (#6994, #7347).

One owner for the question "what grant is this running tech-lead run under?",
asked by the two paths that bring a tech-lead run back without its producer: a
restart (``SessionRestorer``) and a validation retry. The run's own create-once
launch record answers; the anchor's labels and title answer only for a run
that has no record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.session_kind import SessionKind
from ..domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
    health_review_flavor_if_anchored,
)

if TYPE_CHECKING:
    from ..domain.session_run import SessionRunIdentity
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


def recover_tech_lead_launch_scope(
    kind: SessionKind,
    config: "Config",
    issue: "Issue",
    tech_lead_authority: "TechLeadAuthorityStore | None",
    run: "SessionRunIdentity",
) -> "TechLeadLaunchScope | None":
    """Rebuild a RESTORED tech-lead session's launch grant from durable truth.

    A session that survives an orchestrator restart has no in-memory producer to
    hand it a :class:`TechLeadLaunchScope`, and before #6994 round 1 F3 it was
    restored without one — which quietly downgraded a running whole-board review
    to "issue-scoped", so the global barrier lifted and targeted work launched
    alongside an exclusive review, and the dashboard reported the anchor as a
    targeted running issue.

    Everything needed to rebuild the grant is already durable, and this reads it
    in the SAME order the launch path resolves flavor
    (:func:`prepare_tech_lead_session_data`), so a restored run and a fresh one
    can never be classified differently:

    1. the ADR-0031 §4 marker label on the anchor -> ``HEALTH_REVIEW``, whose
       owned cohort comes back from the durable authority ledger;
    2. the batch anchor's title signature -> ``BATCH_REVIEW``;
    3. anything else is an ordinary board issue the tech lead was aimed at, so
       it is a ``FAILURE_INVESTIGATION``.

    Returns ``None`` for a session that is not a tech-lead run at all - which
    the restored run's recorded KIND says (#7347), not the issue's labels.
    """
    from .health_review_trigger import is_batch_anchor_title

    if kind is not SessionKind.TECH_LEAD:
        return None
    # The run's own create-once launch record is the authority (#7347 review
    # r9): the flavor and cohort it was launched with. Labels and titles are
    # mutable and are read only for a run that has no record.
    recorded = (
        tech_lead_authority.load(run_id=run.run_id, session_name=run.session_name)
        if tech_lead_authority is not None
        else None
    )
    if recorded is not None:
        return recorded.launch_scope()
    if health_review_flavor_if_anchored(issue.labels) is not None:
        cohort = (
            tech_lead_authority.load_storm_cohort(anchor_issue_number=issue.number)
            if tech_lead_authority is not None
            else None
        )
        return TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            problem_issue_numbers=tuple(
                sorted({problem.issue_number for problem in cohort or ()})
            ),
        )
    if is_batch_anchor_title(issue.title):
        return TechLeadLaunchScope(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
    return TechLeadLaunchScope(flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)


__all__ = ["recover_tech_lead_launch_scope"]
