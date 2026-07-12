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
from typing import cast

from .triage_artifacts import ACT_LEVEL_TRIAGE_ACTIONS

TRIAGE_ASSIGNMENT_FILENAME = "triage-assignment.json"

# Marker label carried by health-review anchor issues (ADR-0031 §4).
# Labels are crash-safe truth (ADR-0013): the marker is both how the launcher
# derives the HEALTH_REVIEW flavor and how the fact gatherer deduplicates an
# already-open health-review anchor. Single owner — the planner, launcher,
# fact gatherer, and startup recovery all import it from here.
HEALTH_REVIEW_MARKER_LABEL = "triage:health-review"

# Gate label carried by gated triage proposal issues (#6778, ADR-0031 §2
# amendment). Orchestrator-attached at creation; REMOVING it is per-instance
# operator approval. The scheduler's blocking-label classification excludes
# gate-labeled issues from pickup, and the agent-label allowlist rejects it
# as a protected workflow label. Raw (never prefixed), like the marker label:
# the triage subsystem manages its labels without the orchestrator prefix.
PROPOSED_TRIAGE_LABEL = "proposed-triage"

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

    def allowed_act_level_targets(self) -> frozenset[int]:
        """Issue numbers an ACT-LEVEL proposal (reset_retry/kill_hung_session)
        may target — a STRICTER scope than :meth:`allowed_targets`.

        Act-level intents mutate a work ISSUE's runtime (scratch reset, session
        kill). The issue reset owner is handed this number as an
        ``issue_number``, so a batch manifest PR number — or a triage
        bookkeeping anchor — passed here is a confused deputy: it resets the
        wrong entity (#6764 re-review F1). Only a failure investigation owns a
        work issue in scope: its focus issue. Batch and health reviews own no
        resettable work issue (batch manifest entries are PRs; batch/health
        anchors are triage bookkeeping issues), so NO act-level target is in
        scope for them — board-wide remediation must route through the
        scope-free ``create_issue``/``flag_pattern`` proposals instead.
        """
        if self.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION:
            assert self.focus_issue_number is not None  # __post_init__
            return frozenset((self.focus_issue_number,))
        return frozenset()

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


@dataclass(frozen=True)
class StoredTriageOp:
    """Orchestrator-recorded executable payload of a gated triage proposal.

    Recorded create-once in the orchestrator-owned authority store when a
    gated proposal issue is created (#6778): the GitHub issue body is human
    documentation ONLY and is never re-parsed as a command. What the approver
    read and delabeled is exactly what runs — execution consumes THIS record.
    """

    op_type: str  # one of ACT_LEVEL_TRIAGE_ACTIONS
    target_issue_number: int
    rationale: str
    source_run_id: str
    source_session_name: str
    source_action_id: str  # the decision artifact action id (A<n>)
    created_at: str  # ISO-8601 UTC timestamp
    # The target issue's ACTIVE session run id captured at proposal time
    # (#6779 R1). ``kill_hung_session`` consents to terminating exactly THAT
    # generation: the kill executor refuses to act unless the target issue's
    # live session still carries this run id, so a replacement session that
    # started before approval is never killed. Empty for ``reset_retry`` —
    # that op is stale-checked by labels/no-active-session, never bound to a
    # specific generation (a non-empty value there is a bug).
    target_session_id: str = ""
    # The decision findings the approver saw for this op (#6779 R6): forwarded
    # into ``TRIAGE_ACTION_EXECUTED`` so execution correlates to those findings.
    finding_ids: tuple[str, ...] = ()
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported triage op schema_version: {self.schema_version!r}"
            )
        if self.op_type not in ACT_LEVEL_TRIAGE_ACTIONS:
            raise ValueError(
                f"StoredTriageOp op_type must be one of"
                f" {sorted(ACT_LEVEL_TRIAGE_ACTIONS)}, got {self.op_type!r}"
            )
        # Runtime re-checks: from_dict feeds this dataclass persisted JSON,
        # so the declared annotations carry no runtime guarantee here.
        target = cast(object, self.target_issue_number)
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ValueError(
                "StoredTriageOp target_issue_number must be a positive int,"
                f" got {target!r}"
            )
        for field_name in (
            "source_run_id",
            "source_session_name",
            "source_action_id",
            "created_at",
        ):
            value: object = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"StoredTriageOp {field_name} must be a non-empty string,"
                    f" got {value!r}"
                )
        rationale = cast(object, self.rationale)
        if not isinstance(rationale, str):
            raise ValueError(
                f"StoredTriageOp rationale must be a string, got {rationale!r}"
            )
        session_id = cast(object, self.target_session_id)
        if not isinstance(session_id, str):
            raise ValueError(
                "StoredTriageOp target_session_id must be a string,"
                f" got {session_id!r}"
            )
        if self.op_type == "reset_retry" and session_id.strip():
            raise ValueError(
                "StoredTriageOp target_session_id must be empty for reset_retry;"
                " that op is never bound to a specific session generation,"
                f" got {session_id!r}"
            )
        findings = cast(object, self.finding_ids)
        if not isinstance(findings, tuple) or any(
            not isinstance(item, str) for item in findings
        ):
            raise ValueError(
                "StoredTriageOp finding_ids must be a tuple of strings,"
                f" got {findings!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "op_type": self.op_type,
            "target_issue_number": self.target_issue_number,
            "rationale": self.rationale,
            "source_run_id": self.source_run_id,
            "source_session_name": self.source_session_name,
            "source_action_id": self.source_action_id,
            "created_at": self.created_at,
            "target_session_id": self.target_session_id,
            "finding_ids": list(self.finding_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StoredTriageOp":
        """Parse from dict; malformed content fails loudly with ValueError.

        The store is orchestrator-owned, so corruption is a bug, never agent
        input to fail-safe around (mirrors TriageLaunchAuthority.from_dict).
        """
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"triage op schema_version must be an int, got {raw_schema!r}"
            )
        raw_findings = data.get("finding_ids", [])
        if not isinstance(raw_findings, list):
            raise ValueError(
                f"triage op finding_ids must be a list, got {raw_findings!r}"
            )
        return cls(
            op_type=str(data.get("op_type")),
            target_issue_number=data.get("target_issue_number"),  # type: ignore[arg-type]
            rationale=str(data.get("rationale", "")),
            source_run_id=str(data.get("source_run_id", "")),
            source_session_name=str(data.get("source_session_name", "")),
            source_action_id=str(data.get("source_action_id", "")),
            created_at=str(data.get("created_at", "")),
            target_session_id=str(data.get("target_session_id", "")),
            finding_ids=tuple(str(item) for item in raw_findings),
            schema_version=raw_schema,
        )


@dataclass(frozen=True)
class ApprovedTriageOp:
    """A stored op whose proposal issue no longer carries the gate label.

    Classified by the fact gatherer from the SAME open-issue scan that finds
    triage anchors (#6778): an open issue with a stored op but without
    ``PROPOSED_TRIAGE_LABEL`` was approved by the operator. The planner turns
    each into the op's execution action; the applier re-validates
    preconditions and finalizes the proposal issue.
    """

    proposal_issue_number: int
    op: StoredTriageOp
