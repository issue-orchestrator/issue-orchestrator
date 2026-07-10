"""Triage session flavor and assignment (ADR-0031).

Two triage variants share one launch path: batch PR review (audit the
orchestrator-prepared PR manifest) and failure investigation (diagnose one
failed issue). The :class:`TriageAssignment` written at launch is the
authoritative record of which variant a session was given, so the prompt and
the completion planner act on the assignment instead of guessing from session
naming (both variants run in ``issue-{N}`` terminals).
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

TRIAGE_ASSIGNMENT_FILENAME = "triage-assignment.json"

_SCHEMA_VERSION = 1


class TriageSessionFlavor(str, Enum):
    """Which kind of triage work a session was launched to do."""

    BATCH_REVIEW = "batch_review"
    FAILURE_INVESTIGATION = "failure_investigation"


@dataclass(frozen=True)
class TriageAssignment:
    """Launch-time record of a triage session's assignment.

    ``focus_issue_number``/``focus_reason`` name the single issue a
    failure-investigation session must diagnose; batch reviews carry neither
    (their scope is the PR manifest).
    """

    flavor: TriageSessionFlavor
    focus_issue_number: int | None = None
    focus_reason: str = ""
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported triage assignment schema_version: {self.schema_version!r}"
            )
        if (
            self.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION
            and self.focus_issue_number is None
        ):
            raise ValueError(
                "TriageAssignment with flavor=failure_investigation requires "
                "focus_issue_number"
            )

    def to_dict(self) -> dict[str, object]:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "flavor": self.flavor.value,
            "focus_issue_number": self.focus_issue_number,
            "focus_reason": self.focus_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TriageAssignment":
        """Parse from dict; malformed content fails loudly with ValueError."""
        raw_flavor = data.get("flavor")
        try:
            flavor = TriageSessionFlavor(raw_flavor)
        except ValueError:
            raise ValueError(
                f"Unknown triage assignment flavor: {raw_flavor!r}"
            ) from None
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"triage assignment schema_version must be an int, got {raw_schema!r}"
            )
        focus_issue_number = data.get("focus_issue_number")
        if focus_issue_number is not None and (
            isinstance(focus_issue_number, bool)
            or not isinstance(focus_issue_number, int)
        ):
            raise ValueError(
                "triage assignment focus_issue_number must be an int or null, "
                f"got {focus_issue_number!r}"
            )
        focus_reason = data.get("focus_reason", "")
        if not isinstance(focus_reason, str):
            raise ValueError(
                f"triage assignment focus_reason must be a string, got {focus_reason!r}"
            )
        return cls(
            flavor=flavor,
            focus_issue_number=focus_issue_number,
            focus_reason=focus_reason,
            schema_version=raw_schema,
        )

    def write(self, path: Path) -> None:
        """Write assignment to file, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> "TriageAssignment":
        """Read assignment from file; malformed content raises ValueError."""
        return cls.from_dict(json.loads(path.read_text()))
