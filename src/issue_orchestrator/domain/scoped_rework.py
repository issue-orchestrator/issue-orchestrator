"""Immutable evidence and consent for branch-preserving PR rework."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, cast

from .session_run import SessionRunIdentity


@dataclass(frozen=True, slots=True)
class ReworkTarget:
    """Trusted PR facts captured before the reviewing agent starts."""

    repository: str
    pr_number: int
    issue_number: int
    head_sha: str
    branch: str
    pr_labels: tuple[str, ...]
    issue_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("repository", "head_sha", "branch"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ReworkTarget requires {name}")
        if len(self.repository.split("/")) != 2:
            raise ValueError("ReworkTarget repository must be owner/repo")
        for number in (self.pr_number, self.issue_number):
            if type(number) is not int or number <= 0:
                raise ValueError("ReworkTarget requires positive PR and issue numbers")
        for labels in (cast(object, self.pr_labels), cast(object, self.issue_labels)):
            if not isinstance(labels, tuple) or any(
                not isinstance(x, str) for x in labels
            ):
                raise ValueError("ReworkTarget labels must be tuples of strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "pr_labels": list(self.pr_labels),
            "issue_labels": list(self.issue_labels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReworkTarget:
        return cls(
            repository=data["repository"],
            pr_number=data["pr_number"],
            issue_number=data["issue_number"],
            head_sha=data["head_sha"],
            branch=data["branch"],
            pr_labels=tuple(data["pr_labels"]),
            issue_labels=tuple(data["issue_labels"]),
        )


@dataclass(frozen=True, slots=True)
class ReworkRequest:
    """Executable instruction bound to the reviewed head and report contents."""

    target: ReworkTarget
    evidence_identity: str
    report: str
    feedback: str

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.target), ReworkTarget):
            raise ValueError("ReworkRequest requires a trusted target")
        for value in (
            cast(object, self.evidence_identity),
            cast(object, self.report),
            cast(object, self.feedback),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("ReworkRequest requires evidence, report and feedback")

    @property
    def key(self) -> str:
        identity = [
            self.target.repository,
            self.target.pr_number,
            self.target.head_sha,
            self.evidence_identity,
        ]
        return hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "evidence_identity": self.evidence_identity,
            "report": self.report,
            "feedback": self.feedback,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReworkRequest:
        return cls(
            target=ReworkTarget.from_dict(data["target"]),
            evidence_identity=data["evidence_identity"],
            report=data["report"],
            feedback=data["feedback"],
        )


@dataclass(frozen=True, slots=True)
class ReworkReceipt:
    """Durable reconciliation state, independent of the proposal's lifetime."""

    request: ReworkRequest
    status: str
    forward_issue_number: int = 0
    proposal_issue_number: int = 0
    detail: str = ""
    attempt: SessionRunIdentity | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            "executing",
            "queued",
            "active",
            "completed",
            "forward_fix",
            "stale",
            "failed",
        }:
            raise ValueError(f"Unknown rework status: {self.status}")
        if self.status == "forward_fix" and self.forward_issue_number <= 0:
            raise ValueError("forward_fix requires its durable issue identity")

    @property
    def was_rejected(self) -> bool:
        return self.status == "stale"

    @property
    def was_unsuccessful(self) -> bool:
        return self.status == "failed"

    @property
    def requires_reconciliation(self) -> bool:
        return self.status in {"executing", "queued"} and self.attempt is None

    @property
    def has_completed_work(self) -> bool:
        return self.status == "completed"

    @property
    def has_forward_work(self) -> bool:
        return self.status == "forward_fix"

    @property
    def has_claimed_work(self) -> bool:
        return self.status in {"executing", "active"} and self.attempt is not None

    @property
    def has_queued_work(self) -> bool:
        return self.status == "queued"

    def consumed_by(self, identity: SessionRunIdentity) -> bool:
        return self.status in {"executing", "active"} and self.attempt == identity

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status,
            "forward_issue_number": self.forward_issue_number,
            "proposal_issue_number": self.proposal_issue_number,
            "detail": self.detail,
            "attempt": asdict(self.attempt) if self.attempt is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReworkReceipt:
        return cls(
            ReworkRequest.from_dict(data["request"]),
            data["status"],
            data["forward_issue_number"],
            data.get("proposal_issue_number", 0),
            data.get("detail", ""),
            SessionRunIdentity(**data["attempt"])
            if data.get("attempt") is not None
            else None,
        )


@dataclass(frozen=True, slots=True)
class TechLeadProposalCommand:
    proposal_issue_number: int
    decision: str

    def __post_init__(self) -> None:
        if (
            type(self.proposal_issue_number) is not int
            or self.proposal_issue_number <= 0
        ):
            raise ValueError("A proposal command requires a positive issue number")
        if self.decision not in {"approve", "decline"}:
            raise ValueError("A proposal command must approve or decline")


@dataclass(frozen=True, slots=True)
class TechLeadProposalCommandOutcome:
    outcome: str
    detail: str
    proposal_issue_number: int
