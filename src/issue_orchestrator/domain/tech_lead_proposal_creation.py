"""Durable creation intent for the existing stored-op proposal lifecycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from .tech_lead_session import StoredTechLeadOp


def proposal_creation_key(op: StoredTechLeadOp) -> str:
    if op.rework_request is not None:
        return op.rework_request.key
    return hashlib.sha256(
        json.dumps(
            [
                op.source_run_id,
                op.source_session_name,
                op.source_action_id,
            ]
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProposalCreationAuthority:
    """Original anchor expectations, independent of a later recovery command."""

    anchor_issue_number: int
    required_labels: tuple[str, ...]
    forbidden_labels: tuple[str, ...]
    required_pr_state: str | None

    def __post_init__(self) -> None:
        if self.anchor_issue_number <= 0:
            raise ValueError("Proposal creation requires its original anchor")

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_issue_number": self.anchor_issue_number,
            "required_labels": list(self.required_labels),
            "forbidden_labels": list(self.forbidden_labels),
            "required_pr_state": self.required_pr_state,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProposalCreationAuthority:
        return cls(
            value["anchor_issue_number"],
            tuple(value["required_labels"]),
            tuple(value["forbidden_labels"]),
            value["required_pr_state"],
        )


@dataclass(frozen=True, slots=True)
class PendingTechLeadProposal:
    """Original instruction plus an unguessable remote attribution marker."""

    op: StoredTechLeadOp
    title: str
    body: str
    labels: tuple[str, ...]
    milestone: int | None
    marker: str
    # Legacy intents can adopt an attributed issue, but cannot authorize a create.
    creation_authority: ProposalCreationAuthority | None = None

    @property
    def key(self) -> str:
        return proposal_creation_key(self.op)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op.to_dict(),
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "milestone": self.milestone,
            "marker": self.marker,
            "creation_authority": self.creation_authority.to_dict()
            if self.creation_authority
            else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PendingTechLeadProposal:
        return cls(
            StoredTechLeadOp.from_dict(value["op"]),
            value["title"],
            value["body"],
            tuple(value["labels"]),
            value["milestone"],
            value["marker"],
            ProposalCreationAuthority.from_dict(value["creation_authority"])
            if value.get("creation_authority") is not None
            else None,
        )
