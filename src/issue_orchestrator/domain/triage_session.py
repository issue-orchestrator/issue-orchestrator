"""Triage session flavor, assignment, and launch authority (ADR-0031).

Three triage variants share one launch path: batch PR review (audit the
orchestrator-prepared PR manifest), failure investigation (diagnose one
failed issue), and the periodic health review (walk the board snapshot,
ADR-0031 §4). The :class:`TriageAssignment` written at launch tells the
*agent* which variant its session is (all variants run in ``issue-{N}``
terminals).

Trust boundary: the assignment file and the PR manifest live inside the
agent-writable worktree, so completion must never treat them as orchestrator
authority — an agent could rewrite them mid-session. The
:class:`TriageLaunchAuthority` record captures the same launch scope
(flavor, focus, manifest PR set, anchor) for persistence in an
orchestrator-owned store outside the worktree; completion reads THAT record
as authority and treats the worktree copies as the agent's reading material
only (tamper evidence when they diverge).
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

TRIAGE_ASSIGNMENT_FILENAME = "triage-assignment.json"

# Marker label carried by health-review anchor issues (ADR-0031 §4).
# Labels are crash-safe truth (ADR-0013): the marker is both how the launcher
# derives the HEALTH_REVIEW flavor and how the fact gatherer deduplicates an
# already-open health-review anchor. Single owner — the planner, launcher,
# fact gatherer, and startup recovery all import it from here.
HEALTH_REVIEW_MARKER_LABEL = "triage:health-review"

_SCHEMA_VERSION = 1


class TriageSessionFlavor(str, Enum):
    """Which kind of triage work a session was launched to do."""

    BATCH_REVIEW = "batch_review"
    FAILURE_INVESTIGATION = "failure_investigation"
    HEALTH_REVIEW = "health_review"


@dataclass(frozen=True)
class TriageAssignment:
    """Launch-time record of a triage session's assignment.

    ``focus_issue_number``/``focus_reason`` name the single issue a
    failure-investigation session must diagnose; batch and health reviews
    carry neither (their scope is the PR manifest / the board snapshot).
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


@dataclass(frozen=True)
class TriageLaunchAuthority:
    """Orchestrator-owned launch scope for one triage session run.

    Persisted OUTSIDE the agent-writable worktree at launch time and read
    back at completion as the sole authority for the session's flavor, focus
    issue, manifest PR set, and anchor issue. Completion effects (labels,
    close, decision-target scope) key off this record; the worktree copies
    exist only for the agent to read.
    """

    flavor: TriageSessionFlavor
    anchor_issue_number: int
    focus_issue_number: int | None = None
    manifest_pr_numbers: tuple[int, ...] = ()
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported triage authority schema_version: {self.schema_version!r}"
            )
        if (
            self.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION
            and self.focus_issue_number is None
        ):
            raise ValueError(
                "TriageLaunchAuthority with flavor=failure_investigation requires "
                "focus_issue_number"
            )
        if self.flavor is TriageSessionFlavor.HEALTH_REVIEW and (
            self.focus_issue_number is not None or self.manifest_pr_numbers
        ):
            raise ValueError(
                "TriageLaunchAuthority with flavor=health_review carries no "
                "focus issue or manifest PRs; its scope is the anchor issue only "
                "(ADR-0031 §4)"
            )

    def allowed_targets(self) -> frozenset[int]:
        """Issue/PR numbers a decision from this session may target.

        Failure investigations may only address their focus issue; health
        reviews may only address their anchor issue (the report's home,
        ADR-0031 §4); batch reviews may address the audited manifest PRs
        plus the anchor tracking issue. ``create_issue``/``flag_pattern``
        proposals carry no target and are scope-free by construction.
        """
        if self.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION:
            assert self.focus_issue_number is not None  # __post_init__
            return frozenset((self.focus_issue_number,))
        if self.flavor is TriageSessionFlavor.HEALTH_REVIEW:
            return frozenset((self.anchor_issue_number,))
        return frozenset((*self.manifest_pr_numbers, self.anchor_issue_number))

    def matches_assignment(self, assignment: TriageAssignment) -> bool:
        """True when the agent-visible assignment copy mirrors this authority."""
        return (
            assignment.flavor is self.flavor
            and assignment.focus_issue_number == self.focus_issue_number
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "flavor": self.flavor.value,
            "anchor_issue_number": self.anchor_issue_number,
            "focus_issue_number": self.focus_issue_number,
            "manifest_pr_numbers": list(self.manifest_pr_numbers),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TriageLaunchAuthority":
        """Parse from dict; malformed content fails loudly with ValueError."""
        raw_flavor = data.get("flavor")
        try:
            flavor = TriageSessionFlavor(raw_flavor)
        except ValueError:
            raise ValueError(
                f"Unknown triage authority flavor: {raw_flavor!r}"
            ) from None
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"triage authority schema_version must be an int, got {raw_schema!r}"
            )
        anchor = data.get("anchor_issue_number")
        if isinstance(anchor, bool) or not isinstance(anchor, int):
            raise ValueError(
                f"triage authority anchor_issue_number must be an int, got {anchor!r}"
            )
        focus = data.get("focus_issue_number")
        if focus is not None and (isinstance(focus, bool) or not isinstance(focus, int)):
            raise ValueError(
                "triage authority focus_issue_number must be an int or null, "
                f"got {focus!r}"
            )
        raw_prs = data.get("manifest_pr_numbers", [])
        if not isinstance(raw_prs, list) or any(
            isinstance(pr, bool) or not isinstance(pr, int) for pr in raw_prs
        ):
            raise ValueError(
                f"triage authority manifest_pr_numbers must be a list of ints, got {raw_prs!r}"
            )
        return cls(
            flavor=flavor,
            anchor_issue_number=anchor,
            focus_issue_number=focus,
            manifest_pr_numbers=tuple(raw_prs),
            schema_version=raw_schema,
        )
