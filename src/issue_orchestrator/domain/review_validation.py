"""Immutable validation evidence handed to a review exchange."""

from dataclasses import dataclass, field
import json
from typing import Any

from .validated_work import require_text


@dataclass(frozen=True, slots=True)
class ReviewValidationEvidence:
    """Owner-held validation bytes and the review subject they attest.

    Files written into a coder or reviewer worktree are projections for agents
    to inspect.  They never become authority again after this value is built.
    """

    result_bytes: bytes = field(repr=False)
    head_sha: str
    passed: bool

    def __post_init__(self) -> None:
        if type(self.result_bytes) is not bytes:
            raise TypeError("validation evidence must contain immutable bytes")
        require_text(self.head_sha, "validation evidence head")
        if type(self.passed) is not bool:
            raise TypeError("validation evidence outcome must be boolean")
        try:
            payload = json.loads(self.result_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("validation evidence must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("validation evidence must be a JSON object")
        if payload.get("head_sha") != self.head_sha:
            raise ValueError("validation evidence head differs from its payload")
        if payload.get("passed") is not self.passed:
            raise ValueError("validation evidence outcome differs from its payload")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "ReviewValidationEvidence":
        return cls(
            result_bytes=(json.dumps(payload, indent=2) + "\n").encode(),
            head_sha=payload["head_sha"],
            passed=payload["passed"],
        )


def evidence_subject(
    evidence: ReviewValidationEvidence | None,
) -> tuple[str | None, bool]:
    if evidence is None:
        return None, False
    return evidence.head_sha, not evidence.passed
