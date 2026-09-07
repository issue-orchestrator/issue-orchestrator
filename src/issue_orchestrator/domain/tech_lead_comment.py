"""Exact tech-lead comment intent retained across decision lowering."""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .tech_lead_artifacts import ProposedTechLeadAction


def provenance_footer(action_id: str, finding_ids: tuple[str, ...]) -> str:
    findings = ", ".join(finding_ids) or "none"
    return f"\n\n---\n*Proposed by tech_lead session (action {action_id}; findings: {findings}) — ADR-0031.*"


@dataclass(frozen=True)
class TechLeadCommentIntent:
    action_id: str
    body: str
    finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.body.strip():
            raise ValueError("required diagnosis needs its source identity and full body")

    @classmethod
    def from_action(cls, action: ProposedTechLeadAction) -> TechLeadCommentIntent:
        return cls(action.id, action.body or "", action.finding_ids)

    @property
    def comment(self) -> str:
        return self.body + provenance_footer(self.action_id, self.finding_ids)
