"""Tech Lead board projection: orchestrator-authored rung-1 visibility (#6781).

A frozen view over the orchestrator-owned tech_lead ledgers and the anchor
scan's facts — open gated proposals (op/target/age), open pattern case files
(ranked by comment cadence), per-area case-file counts, and the last
health-review time — plus a deterministic markdown renderer. Everything here
is orchestrator-authored: no agent prose ever reaches the board.

Build inputs come from data the tick already holds (the authority store's
rows and the ONE anchor scan's classification); building the view makes zero
GitHub calls. The renderer is pure and deterministic: the same view always
produces the same markdown, so the publisher can throttle writes by content
comparison and tests can golden-match the output exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..domain.tech_lead_session import StoredTechLeadOp, TechLeadCaseFileSummary


@dataclass(frozen=True)
class TechLeadBoardProposal:
    """One open gated tech_lead proposal, as shown on the board."""

    proposal_issue_number: int
    op_type: str
    target_issue_number: int
    age_hours: int


@dataclass(frozen=True)
class TechLeadBoardCaseFile:
    """One open pattern case file, as shown on the board."""

    issue_number: int
    title: str
    comment_count: int
    updated_at: str
    area: str


@dataclass(frozen=True)
class TechLeadBoardView:
    """Frozen board projection; input to :func:`render_tech_lead_board_md`."""

    open_proposals: tuple[TechLeadBoardProposal, ...]
    case_files: tuple[TechLeadBoardCaseFile, ...]
    area_counts: tuple[tuple[str, int], ...]
    last_health_review: str  # ISO timestamp; "" when never


def _proposal_age_hours(created_at: str, now: datetime) -> int:
    """Whole hours since the op was recorded; 0 for unparseable timestamps.

    Coarse on purpose: hour granularity keeps the rendered board stable
    within an hour, so the publisher's content-comparison throttle holds.
    """
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 3600))


def build_tech_lead_board_view(
    *,
    ops: Sequence[tuple[int, "StoredTechLeadOp"]],
    case_files: Sequence["TechLeadCaseFileSummary"],
    area_counts: Sequence[tuple[str, int]],
    last_health_review_at: float,
    now: datetime,
) -> TechLeadBoardView:
    """Project the ledgers + scan facts onto the board.

    Proposals sort by issue number (stable audit order); case files rank by
    comment count (the severity signal), then most recently updated, then
    issue number — the same priority order a health review should read
    them in.
    """
    proposals = tuple(
        TechLeadBoardProposal(
            proposal_issue_number=issue_number,
            op_type=op.op_type,
            target_issue_number=op.target_issue_number,
            age_hours=_proposal_age_hours(op.created_at, now),
        )
        for issue_number, op in sorted(ops, key=lambda item: item[0])
    )
    ranked = sorted(case_files, key=lambda item: (item.updated_at, item.issue_number), reverse=True)
    ranked.sort(key=lambda item: item.comment_count, reverse=True)
    return TechLeadBoardView(
        open_proposals=proposals,
        case_files=tuple(
            TechLeadBoardCaseFile(
                issue_number=item.issue_number,
                title=item.title,
                comment_count=item.comment_count,
                updated_at=item.updated_at,
                area=item.area,
            )
            for item in ranked
        ),
        area_counts=tuple(area_counts),
        last_health_review=(
            datetime.fromtimestamp(last_health_review_at, tz=timezone.utc).isoformat()
            if last_health_review_at > 0
            else ""
        ),
    )


def render_tech_lead_board_md(view: TechLeadBoardView) -> str:
    """Render the board to markdown. Deterministic; orchestrator-authored."""
    lines: list[str] = [
        "# Tech Lead Board",
        "",
        "Orchestrator-authored projection of the tech_lead ledgers"
        " (ADR-0031 / #6781).",
        "",
        f"Last health review: {view.last_health_review or 'never'}",
        "",
        "## Open proposals",
        "",
    ]
    if view.open_proposals:
        lines.extend(
            [
                "| Proposal | Operation | Target | Age |",
                "|---|---|---|---|",
                *(
                    f"| #{item.proposal_issue_number} | `{item.op_type}`"
                    f" | #{item.target_issue_number} | {item.age_hours}h |"
                    for item in view.open_proposals
                ),
            ]
        )
    else:
        lines.append("None.")
    lines.extend(["", "## Open pattern case files", ""])
    if view.case_files:
        lines.extend(
            [
                "| Case file | Title | Comments | Updated | Area |",
                "|---|---|---|---|---|",
                *(
                    f"| #{item.issue_number} | {_markdown_cell(item.title)}"
                    f" | {item.comment_count} | {item.updated_at or 'unknown'}"
                    f" | {_markdown_cell(item.area or 'unclassified')} |"
                    for item in view.case_files
                ),
            ]
        )
    else:
        lines.append("None.")
    lines.extend(["", "## Case files by area", ""])
    if view.area_counts:
        lines.extend(
            f"- {_markdown_cell(area)}: {count}" for area, count in view.area_counts
        )
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _markdown_cell(value: str) -> str:
    """Keep issue/label text from breaking the deterministic table shape."""
    return value.replace("\r", " ").replace("\n", " ").replace("|", r"\|")
