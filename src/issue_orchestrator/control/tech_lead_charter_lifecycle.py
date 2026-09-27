"""Link a gated proposal's fate back to the charter decision that filed it (#7330).

A gated act-level proposal carries its stored op, which names the run and the
decision action id it came from. When the operator approves it (and the op is
applied, or found stale) or closes it unapproved, the charter decision record is
updated here, so a reader sees what became of an item the charter sent to the
approval gate. Both hooks run where the op ledger row is consumed, BEFORE the
row is discarded — the row is the only link from proposal issue to decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.tech_lead_charter_decisions import CharterProposalLifecycle

if TYPE_CHECKING:
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


def _link(
    authority: "TechLeadAuthorityStore",
    proposal_issue_number: int,
    lifecycle: CharterProposalLifecycle,
) -> int:
    op = authority.load_op(issue_number=proposal_issue_number)
    if op is None:
        return 0
    return authority.charter_ledger.link_proposal_outcome(
        run_id=op.source_run_id,
        action_id=op.source_action_id,
        proposal_issue_number=proposal_issue_number,
        lifecycle=lifecycle,
        at=datetime.now(timezone.utc).isoformat(),
    )


def link_approved_proposal(
    authority: "TechLeadAuthorityStore", proposal_issue_number: int, *, applied: bool
) -> int:
    """The operator approved it; the op ran (``applied``) or was found stale."""
    return _link(
        authority,
        proposal_issue_number,
        CharterProposalLifecycle.APPROVED_APPLIED
        if applied
        else CharterProposalLifecycle.APPROVED_STALE,
    )


def link_declined_proposal(
    authority: "TechLeadAuthorityStore", proposal_issue_number: int
) -> int:
    """The proposal issue closed without approval."""
    return _link(authority, proposal_issue_number, CharterProposalLifecycle.DECLINED)
