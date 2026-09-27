"""The charter rows of the tech-lead board (#7330).

Active dials come from the charter policy; outcome counts come from the
persisted decision ledger (the recent window), so the board reports what was
actually decided rather than what the current charter would decide now.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from ..domain.tech_lead_charter import CharterOutcome, CharterRole
from ..view_models.tech_lead_board import TechLeadBoardCharterRole

if TYPE_CHECKING:
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_charter_policy import TechLeadCharterPolicy

#: The recent-decision window the board counts over.
CHARTER_BOARD_WINDOW = 500


def charter_board_rows(
    policy: "TechLeadCharterPolicy | None",
    authority: "TechLeadAuthorityStore | None",
) -> tuple[TechLeadBoardCharterRole, ...]:
    if policy is None:
        return ()
    recent = (
        authority.charter_ledger.list_recent(limit=CHARTER_BOARD_WINDOW)
        if authority is not None
        else ()
    )
    counts = Counter((row.role, row.outcome) for row in recent)
    rows: list[TechLeadBoardCharterRole] = []
    for role in CharterRole:
        charter = policy.charter.for_role(role)
        dials = (
            f"{charter.depth.value} / {charter.authority.value}"
            if charter.enabled
            else "disabled"
        )
        rows.append(
            TechLeadBoardCharterRole(
                role=role.value,
                dials=dials,
                executed=counts[(role, CharterOutcome.EXECUTED)],
                proposed=counts[(role, CharterOutcome.PROPOSED)],
                refused_destructive=counts[(role, CharterOutcome.REFUSED_DESTRUCTIVE)],
                advice_only=counts[(role, CharterOutcome.ADVICE_ONLY)],
            )
        )
    return tuple(rows)
