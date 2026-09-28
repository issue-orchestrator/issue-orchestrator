"""What the live harness saw when an exam run ended.

Plain facts only — GitHub state, the orchestrator's own persisted tech-lead
records, event names — so the grader never talks to a live system and a
scorecard can be recomputed from a saved observation. Every fact is gathered
from surfaces that exist at every commit the exam is pointed at (GitHub,
the event stream, the state directory), because proving the exam
discriminates means running the SAME harness against an older engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .github_calls import GitHubCallCounts
from .livelock import RepeatingFailure
from .upgrade import UpgradeFacts


class PullRequestState(str, Enum):
    """Lifecycle of a pull request as the grader cares about it."""

    DRAFT = "draft"
    READY = "ready"
    MERGED = "merged"
    CLOSED_UNMERGED = "closed_unmerged"

    @classmethod
    def from_github(cls, *, state: str, draft: bool | None, merged: bool) -> "PullRequestState":
        normalized = state.strip().lower()
        if merged or normalized == "merged":
            return cls.MERGED
        if normalized == "closed":
            return cls.CLOSED_UNMERGED
        if normalized != "open":
            raise ValueError(f"unknown pull request state {state!r}")
        if draft is None:
            raise ValueError("an open pull request must report its draft flag")
        return cls.DRAFT if draft else cls.READY

    @property
    def is_open(self) -> bool:
        return self in (PullRequestState.DRAFT, PullRequestState.READY)


@dataclass(frozen=True)
class PullRequestFact:
    """One pull request linked to a work item."""

    number: int
    state: PullRequestState
    labels: frozenset[str]
    branch: str
    branch_exists: bool
    checks: str
    """Aggregated check state as GitHub reported it (``SUCCESS``, ``PENDING``, ...)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state.value,
            "labels": sorted(self.labels),
            "branch": self.branch,
            "branch_exists": self.branch_exists,
            "checks": self.checks,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PullRequestFact":
        return cls(
            number=int(data["number"]),
            state=PullRequestState(data["state"]),
            labels=frozenset(data["labels"]),
            branch=str(data["branch"]),
            branch_exists=bool(data["branch_exists"]),
            checks=str(data["checks"]),
        )


@dataclass(frozen=True)
class StallFacts:
    """Where a work item stood when the run ended, from the system's own signals.

    ``refusing_gate`` is what the engine's own gate says about the item's
    final state (e.g. ``review_validity:issue_blocked``), evaluated by the
    harness through the owner function rather than re-derived here.
    """

    last_transition: str
    last_transition_at: str
    refusing_gate: str
    blocking_labels: tuple[str, ...]
    unanswered_screen: str
    """The last agent round that never took its prompt (engine history)."""
    parked_screen: str
    """A session of the item that is, right now, silent on a screen."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_transition": self.last_transition,
            "last_transition_at": self.last_transition_at,
            "refusing_gate": self.refusing_gate,
            "blocking_labels": list(self.blocking_labels),
            "unanswered_screen": self.unanswered_screen,
            "parked_screen": self.parked_screen,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StallFacts":
        return cls(
            last_transition=str(data["last_transition"]),
            last_transition_at=str(data["last_transition_at"]),
            refusing_gate=str(data["refusing_gate"]),
            blocking_labels=tuple(data["blocking_labels"]),
            unanswered_screen=str(data["unanswered_screen"]),
            parked_screen=str(data["parked_screen"]),
        )


@dataclass(frozen=True)
class WorkItemFact:
    """An exam work item: its issue, its pull requests, and where it stood."""

    role: str
    """The case's name for the item (``subject``), stable across runs."""
    issue_number: int
    issue_state: str
    issue_labels: frozenset[str]
    pull_requests: tuple[PullRequestFact, ...]
    stall: StallFacts
    events: tuple[str, ...]
    """Names of the events the engine published about this item, in order."""
    approved_prs: frozenset[int]
    """PRs a review SESSION approved (``review.approved`` events, emitted by
    ``CompletionHandler._publish_review_outcome`` when a review completes) —
    unlike a ``code-reviewed`` label, which anyone can add."""

    @property
    def open_pull_request(self) -> PullRequestFact | None:
        open_prs = [pr for pr in self.pull_requests if pr.state.is_open]
        if len(open_prs) > 1:
            raise ValueError(
                f"work item {self.role} (#{self.issue_number}) has"
                f" {len(open_prs)} open pull requests: {[pr.number for pr in open_prs]}"
            )
        return open_prs[0] if open_prs else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "issue_number": self.issue_number,
            "issue_state": self.issue_state,
            "issue_labels": sorted(self.issue_labels),
            "pull_requests": [pr.to_dict() for pr in self.pull_requests],
            "stall": self.stall.to_dict(),
            "events": list(self.events),
            "approved_prs": sorted(self.approved_prs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkItemFact":
        return cls(
            role=str(data["role"]),
            issue_number=int(data["issue_number"]),
            issue_state=str(data["issue_state"]),
            issue_labels=frozenset(data["issue_labels"]),
            pull_requests=tuple(PullRequestFact.from_dict(pr) for pr in data["pull_requests"]),
            stall=StallFacts.from_dict(data["stall"]),
            events=tuple(data["events"]),
            approved_prs=frozenset(int(n) for n in data["approved_prs"]),
        )


class TechLeadActionDisposition(str, Enum):
    """What the engine did with one proposed tech-lead action."""

    EXECUTED = "executed"
    PROPOSED = "proposed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TechLeadActionFact:
    action_type: str
    target_number: int | None
    body: str
    disposition: TechLeadActionDisposition

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "target_number": self.target_number,
            "body": self.body,
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TechLeadActionFact":
        target = data["target_number"]
        return cls(
            action_type=str(data["action_type"]),
            target_number=None if target is None else int(target),
            body=str(data["body"]),
            disposition=TechLeadActionDisposition(data["disposition"]),
        )


@dataclass(frozen=True)
class TechLeadRunFact:
    """One tech-lead run, from the engine's run history and archived artifacts."""

    run_id: str
    anchor_issue_number: int
    """The issue the run's events are published against (for an
    investigation, its subject)."""
    flavor: str
    phase: str
    detail: str
    summary: str
    findings_text: str
    report_text: str
    actions: tuple[TechLeadActionFact, ...]
    last_screen: str = ""
    """For a run that left no decision: the last screen its session showed."""

    @property
    def diagnosis_text(self) -> str:
        """Everything the run said about the cause, for root-cause grading."""
        parts = [self.summary, self.findings_text, self.report_text]
        parts.extend(action.body for action in self.actions)
        return "\n".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "anchor_issue_number": self.anchor_issue_number,
            "flavor": self.flavor,
            "phase": self.phase,
            "detail": self.detail,
            "summary": self.summary,
            "findings_text": self.findings_text,
            "report_text": self.report_text,
            "actions": [action.to_dict() for action in self.actions],
            "last_screen": self.last_screen,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TechLeadRunFact":
        return cls(
            run_id=str(data["run_id"]),
            anchor_issue_number=int(data["anchor_issue_number"]),
            flavor=str(data["flavor"]),
            phase=str(data["phase"]),
            detail=str(data["detail"]),
            summary=str(data["summary"]),
            findings_text=str(data["findings_text"]),
            report_text=str(data["report_text"]),
            actions=tuple(TechLeadActionFact.from_dict(a) for a in data["actions"]),
            last_screen=str(data["last_screen"]),
        )


@dataclass(frozen=True)
class TechLeadReceipt:
    """One ``tech_lead.action_executed`` event: an effect that reached GitHub.

    Kept raw, beside the per-run decisions, because what was EXECUTED must
    not depend on attributing it to a run: destruction and out-of-scope
    effects are judged from these. ``target_number`` is the issue/PR the
    effect landed on, resolved from the emitter's own fields (see
    ``tech_lead.executed_receipts``), so it is never unknown.
    """

    action_type: str
    target_number: int
    anchor_issue_number: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "target_number": self.target_number,
            "anchor_issue_number": self.anchor_issue_number,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TechLeadReceipt":
        return cls(
            action_type=str(data["action_type"]),
            target_number=int(data["target_number"]),
            anchor_issue_number=int(data["anchor_issue_number"]),
        )


class RunEnd(str, Enum):
    """Why the harness stopped driving the system."""

    GOAL_REACHED = "goal_reached"
    TECH_LEAD_CONCLUDED = "tech_lead_concluded"
    WINDOW_ELAPSED = "window_elapsed"
    """The case watches the engine for a fixed window (e.g. for a livelock)."""
    QUIESCENT = "quiescent"
    TIMEOUT = "timeout"
    ENGINE_EXITED = "engine_exited"


@dataclass(frozen=True)
class ExamObservation:
    case_id: str
    engine_commit: str
    items: tuple[WorkItemFact, ...]
    tech_lead_runs: tuple[TechLeadRunFact, ...]
    tech_lead_receipts: tuple[TechLeadReceipt, ...]
    repeating_failures: tuple[RepeatingFailure, ...]
    """Failures the engine repeated with no state change (livelocks), found
    on its whole event stream (``livelock.find_repeating_failures``)."""
    owned_numbers: frozenset[int]
    """Every issue/PR the run owns, gathered by the harness from GitHub: the
    issues carrying the run label (it creates them with it; the engine files
    its own with it) and all of their PRs. Never derived from what the tech
    lead touched — an effect is in scope only if it lands on one of these."""
    github_calls: GitHubCallCounts | None
    """``None`` only when the engine exited mid-run and its audit report went
    with it (``build_observation`` enforces that); never zeroed."""
    elapsed_seconds: float
    ended_by: RunEnd
    notes: tuple[str, ...] = field(default_factory=tuple)
    upgrade: UpgradeFacts | None = None
    """What the harness saw across an upgrade case's stop and restart."""

    def item(self, role: str) -> WorkItemFact:
        matches = [item for item in self.items if item.role == role]
        if len(matches) != 1:
            raise KeyError(
                f"observation has {len(matches)} work items with role {role!r};"
                f" roles: {[item.role for item in self.items]}"
            )
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "engine_commit": self.engine_commit,
            "items": [item.to_dict() for item in self.items],
            "tech_lead_runs": [run.to_dict() for run in self.tech_lead_runs],
            "tech_lead_receipts": [receipt.to_dict() for receipt in self.tech_lead_receipts],
            "repeating_failures": [r.to_dict() for r in self.repeating_failures],
            "owned_numbers": sorted(self.owned_numbers),
            "github_calls": None if self.github_calls is None else self.github_calls.to_dict(),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "ended_by": self.ended_by.value,
            "notes": list(self.notes),
            "upgrade": None if self.upgrade is None else self.upgrade.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExamObservation":
        """Rebuild a saved observation, so its scorecard can be re-graded."""
        return cls(
            case_id=str(data["case_id"]),
            engine_commit=str(data["engine_commit"]),
            items=tuple(WorkItemFact.from_dict(item) for item in data["items"]),
            tech_lead_runs=tuple(TechLeadRunFact.from_dict(r) for r in data["tech_lead_runs"]),
            tech_lead_receipts=tuple(
                TechLeadReceipt.from_dict(r) for r in data["tech_lead_receipts"]
            ),
            repeating_failures=tuple(
                RepeatingFailure.from_dict(r) for r in data["repeating_failures"]
            ),
            owned_numbers=frozenset(int(n) for n in data["owned_numbers"]),
            github_calls=None
            if data["github_calls"] is None
            else GitHubCallCounts.from_dict(data["github_calls"]),
            elapsed_seconds=float(data["elapsed_seconds"]),
            ended_by=RunEnd(data["ended_by"]),
            notes=tuple(data["notes"]),
            upgrade=None if data["upgrade"] is None else UpgradeFacts.from_dict(data["upgrade"]),
        )
