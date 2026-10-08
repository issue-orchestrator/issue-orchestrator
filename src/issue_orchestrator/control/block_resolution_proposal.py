"""What a gated ``resolve_block`` proposal tells the operator (#7658).

The proposal issue body is documentation only (the stored op is what runs), so
this says exactly what approving the stored resolution does, in the
operator's words. Kept beside the proposal lifecycle owner, which renders
every other op's body, so that module stays within its size budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.block_resolution import PARENT, ParentDisposition
from ..domain.decision_steps import follow_through_section

if TYPE_CHECKING:
    from ..domain.tech_lead_session import StoredTechLeadOp


def resolution_proposal_section(op: "StoredTechLeadOp") -> str:
    """What approving a ``resolve_block`` does, in the operator's words (#7658)."""
    assert op.resolution is not None
    resolution = op.resolution
    number = op.target_issue_number
    causes = ", ".join(f"`{value}`" for value in sorted(cause.value for cause in resolution.causes))
    evidence = "".join(f"\n- {item}" for item in resolution.evidence)
    return f"""## Resolution of #{number}'s block ({resolution.kind.value}): {resolution.title}

{resolution.body}

### Evidence
{evidence}
{_split_section(op)}{follow_through_section(op.follow_through, subject=number)}
### What approving does

1. Re-checks #{number}: still open, nothing running or claiming it, the causes
   below still recorded and never resolved before, and no human-only work
   (credentials, accounts, provisioning, money, legal) named on it.
2. Files the children above, if any, with their dependency lines.
3. Posts this decision on #{number}, so the session that resumes it works to it.
4. Discharges only these needs-human causes: {causes}. Any other cause keeps
   the label. If you put the block back afterwards, the tech lead never clears
   that cause on #{number} again.

"""


def _split_section(op: "StoredTechLeadOp") -> str:
    assert op.resolution is not None
    resolution = op.resolution
    if not resolution.children:
        return ""
    children = []
    for index, child in enumerate(resolution.children, start=1):
        edge = ""
        if child.edge is not None:
            after = "#" + str(op.target_issue_number) if child.after == PARENT else f"child {child.after}"
            edge = f"Comes after {after} (`{child.edge.directive}:`).\n\n"
        children.append(f"\n#### Child {index}: {child.title}\n\n{edge}{child.body}\n")
    parent = (
        "closed: the children carry all of it"
        if resolution.parent is ParentDisposition.CLOSE
        else "narrowed to the slice it keeps, and requeued"
    )
    return f"\n### Issues approval files\n{''.join(children)}\nThen #{op.target_issue_number} is {parent}.\n"
