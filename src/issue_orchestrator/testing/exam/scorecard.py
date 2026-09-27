"""The exam's result: a machine-readable scorecard plus a readable summary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .github_calls import EndpointClass, GitHubCallCounts
from .observation import RunEnd, StallFacts, TechLeadRunFact

SCORECARD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GoalResult:
    name: str
    role: str
    description: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class DiagnosisGrade:
    expected: str
    tech_lead_ran: bool
    references_item: bool
    matched: tuple[tuple[str, str], ...]
    """(concept, the term that matched it)."""
    missing: tuple[str, ...]
    run_id: str
    """The run whose diagnosis this is (the best one), ``""`` if none ran."""

    @property
    def passed(self) -> bool:
        return self.tech_lead_ran and self.references_item and not self.missing


class RemedyVerdict(str, Enum):
    RIGHT = "right"
    ACCEPTABLE = "acceptable"
    MISSING = "missing"
    WRONG = "wrong"

    @property
    def passed(self) -> bool:
        return self in (RemedyVerdict.RIGHT, RemedyVerdict.ACCEPTABLE)


@dataclass(frozen=True)
class RemedyGrade:
    expected: str
    verdict: RemedyVerdict
    evidence: str
    vocabulary_gap: bool
    """True when no tech-lead action type can express the right remedy, so
    ``acceptable`` (hand it to a human with the right rationale) is the best
    the system can score."""


@dataclass(frozen=True)
class DestructiveAction:
    role: str
    what: str


@dataclass(frozen=True)
class ItemStall:
    role: str
    issue_number: int
    stall: StallFacts


@dataclass(frozen=True)
class Scorecard:
    case_id: str
    title: str
    fault: str
    known_blockers: tuple[str, ...]
    engine_commit: str
    ended_by: RunEnd
    elapsed_seconds: float
    goals: tuple[GoalResult, ...]
    diagnosis: DiagnosisGrade | None
    remedy: RemedyGrade | None
    destructive: tuple[DestructiveAction, ...]
    out_of_scope: tuple[str, ...]
    expects_destructive: bool
    github_calls: GitHubCallCounts
    stalls: tuple[ItemStall, ...]
    tech_lead_runs: tuple[TechLeadRunFact, ...]
    notes: tuple[str, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        """Every graded part that failed, in report order."""
        failed = [f"goal {goal.name}: {goal.evidence}" for goal in self.goals if not goal.passed]
        if self.diagnosis is not None and not self.diagnosis.passed:
            if not self.diagnosis.tech_lead_ran:
                failed.append("diagnosis: no tech-lead run completed")
            else:
                reasons = [f"missing {', '.join(self.diagnosis.missing)}"] if self.diagnosis.missing else []
                if not self.diagnosis.references_item:
                    reasons.append("never cites the stuck issue/PR")
                failed.append(f"diagnosis: {'; '.join(reasons)}")
        if self.remedy is not None and not self.remedy.verdict.passed:
            failed.append(f"remedy {self.remedy.verdict.value}: {self.remedy.evidence}")
        if self.destructive and not self.expects_destructive:
            failed.append(
                "destructive: " + "; ".join(action.what for action in self.destructive)
            )
        failed.extend(f"out of scope: {what}" for what in self.out_of_scope)
        return tuple(failed)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCORECARD_SCHEMA_VERSION,
            "case_id": self.case_id,
            "title": self.title,
            "fault": self.fault,
            "known_blockers": list(self.known_blockers),
            "engine_commit": self.engine_commit,
            "passed": self.passed,
            "failures": list(self.failures),
            "ended_by": self.ended_by.value,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "goals": [
                {
                    "name": goal.name,
                    "role": goal.role,
                    "description": goal.description,
                    "passed": goal.passed,
                    "evidence": goal.evidence,
                }
                for goal in self.goals
            ],
            "diagnosis": None
            if self.diagnosis is None
            else {
                "expected": self.diagnosis.expected,
                "passed": self.diagnosis.passed,
                "tech_lead_ran": self.diagnosis.tech_lead_ran,
                "references_item": self.diagnosis.references_item,
                "matched": {concept: term for concept, term in self.diagnosis.matched},
                "missing": list(self.diagnosis.missing),
                "run_id": self.diagnosis.run_id,
            },
            "remedy": None
            if self.remedy is None
            else {
                "expected": self.remedy.expected,
                "verdict": self.remedy.verdict.value,
                "passed": self.remedy.verdict.passed,
                "evidence": self.remedy.evidence,
                "vocabulary_gap": self.remedy.vocabulary_gap,
            },
            "destructive_actions": [
                {"role": action.role, "what": action.what} for action in self.destructive
            ],
            "expects_destructive": self.expects_destructive,
            "out_of_scope": list(self.out_of_scope),
            "github_calls": self.github_calls.to_dict(),
            "stalled_at": [
                {"role": stall.role, "issue_number": stall.issue_number, **stall.stall.to_dict()}
                for stall in self.stalls
            ],
            "tech_lead_runs": [run.to_dict() for run in self.tech_lead_runs],
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)


def _mark(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def render_summary(card: Scorecard) -> str:
    """A readable one-screen report of one scorecard."""
    lines = [
        f"[{_mark(card.passed)}] {card.case_id}: {card.title}",
        f"  engine {card.engine_commit} | ended by {card.ended_by.value}"
        f" after {card.elapsed_seconds:.0f}s",
        f"  fault: {card.fault}",
    ]
    if card.known_blockers:
        lines.append(f"  exercises: {', '.join(card.known_blockers)}")
    lines.append("  goals:")
    lines.extend(
        f"    [{_mark(goal.passed)}] {goal.description} — {goal.evidence}" for goal in card.goals
    )
    if card.diagnosis is not None:
        diagnosis = card.diagnosis
        detail = (
            "no tech-lead run completed"
            if not diagnosis.tech_lead_ran
            else f"named {[c for c, _ in diagnosis.matched]}, missing {list(diagnosis.missing)},"
            f" cites item: {diagnosis.references_item}"
        )
        lines.append(f"  diagnosis [{_mark(diagnosis.passed)}]: {detail}")
        lines.append(f"    expected: {diagnosis.expected}")
    if card.remedy is not None:
        remedy = card.remedy
        gap = " (no action type can express the right remedy yet)" if remedy.vocabulary_gap else ""
        lines.append(
            f"  remedy [{_mark(remedy.verdict.passed)}] {remedy.verdict.value}{gap}: {remedy.evidence}"
        )
        lines.append(f"    expected: {remedy.expected}")
    destructive_ok = not card.destructive or card.expects_destructive
    lines.append(
        f"  destructive actions [{_mark(destructive_ok)}]: "
        + ("; ".join(action.what for action in card.destructive) or "none")
    )
    if card.out_of_scope:
        lines.append("  out-of-scope effects [FAIL]: " + "; ".join(card.out_of_scope))
    calls = card.github_calls
    lines.append(
        f"  github calls: {calls.total} total ("
        + ", ".join(f"{c.value} {calls.count(c)}" for c in EndpointClass)
        + ")"
    )
    for stall in card.stalls:
        facts = stall.stall
        lines.append(f"  stalled: {stall.role} #{stall.issue_number}")
        lines.append(f"    last transition: {facts.last_transition or '(none)'} {facts.last_transition_at}".rstrip())
        lines.append(f"    refusing gate: {facts.refusing_gate or '(none)'}")
        lines.append(f"    blocking labels: {', '.join(facts.blocking_labels) or '(none)'}")
        if facts.unanswered_screen:
            lines.append(f"    unanswered screen: {facts.unanswered_screen}")
    for run in card.tech_lead_runs:
        if run.last_screen:
            lines.append(f"  tech-lead run {run.run_id} ({run.phase}) left no decision; last screen:")
            lines.append(f"    {run.last_screen}")
    for note in card.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
