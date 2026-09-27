"""The exam's result: a machine-readable scorecard plus a readable summary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .github_calls import EndpointClass, GitHubCallCounts
from .livelock import RepeatingFailure
from .observation import RunEnd, StallFacts, TechLeadRunFact
from .upgrade import UpgradeGrade, WriteKind

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
    evidence_clause: str
    """The one clause that names every root-cause concept together — the
    evidence a human audits, since a lexical grader cannot judge polarity —
    or ``""`` when no single clause does."""

    @property
    def passed(self) -> bool:
        return (
            self.tech_lead_ran
            and self.references_item
            and not self.missing
            and bool(self.evidence_clause)
        )


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
    repeating_failures: tuple[RepeatingFailure, ...]
    expects_destructive: bool
    github_calls: GitHubCallCounts | None
    stalls: tuple[ItemStall, ...]
    tech_lead_runs: tuple[TechLeadRunFact, ...]
    notes: tuple[str, ...]
    upgrade: UpgradeGrade | None = None

    @property
    def failures(self) -> tuple[str, ...]:
        """Every graded part that failed, in report order."""
        failed = [f"goal {goal.name}: {goal.evidence}" for goal in self.goals if not goal.passed]
        if self.ended_by is RunEnd.ENGINE_EXITED:
            # A case the engine did not live through was not passed, whatever
            # the partial state shows.
            failed.insert(0, "engine exited before the case finished")
        if self.diagnosis is not None and not self.diagnosis.passed:
            if not self.diagnosis.tech_lead_ran:
                failed.append("diagnosis: no tech-lead run completed")
            else:
                reasons = [f"missing {', '.join(self.diagnosis.missing)}"] if self.diagnosis.missing else []
                if not self.diagnosis.references_item:
                    reasons.append("never cites the stuck issue/PR")
                if not self.diagnosis.missing and not self.diagnosis.evidence_clause:
                    reasons.append("no single clause connects the cause (concepts only named apart)")
                failed.append(f"diagnosis: {'; '.join(reasons)}")
        if self.remedy is not None and not self.remedy.verdict.passed:
            failed.append(f"remedy {self.remedy.verdict.value}: {self.remedy.evidence}")
        if self.destructive and not self.expects_destructive:
            failed.append(
                "destructive: " + "; ".join(action.what for action in self.destructive)
            )
        failed.extend(f"out of scope: {what}" for what in self.out_of_scope)
        # Every case: an engine repeating a failure with no state change is
        # livelocked, whatever the goals say (the #7345/#7346 class).
        failed.extend(f"livelock: {r.describe()}" for r in self.repeating_failures)
        # A work item still waiting on a screen is not finished, whatever
        # GitHub shows. Round history (unanswered_screen) stays informational.
        failed.extend(
            f"parked: {stall.role} #{stall.issue_number}: {stall.stall.parked_screen}"
            for stall in self.stalls
            if stall.stall.parked_screen
        )
        if self.upgrade is not None:
            failed.extend(f"upgrade: {failure}" for failure in self.upgrade.failures)
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
                "evidence_clause": self.diagnosis.evidence_clause,
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
            "repeating_failures": [r.to_dict() for r in self.repeating_failures],
            "github_calls": None if self.github_calls is None else self.github_calls.to_dict(),
            "stalled_at": [
                {"role": stall.role, "issue_number": stall.issue_number, **stall.stall.to_dict()}
                for stall in self.stalls
            ],
            "tech_lead_runs": [run.to_dict() for run in self.tech_lead_runs],
            "notes": list(self.notes),
            "upgrade": None if self.upgrade is None else self.upgrade.to_dict(),
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
    lines.extend(_answer_lines(card))
    lines.extend(_upgrade_lines(card))
    lines.extend(_effect_lines(card))
    for stall in card.stalls:
        lines.extend(_stall_lines(stall))
    for run in card.tech_lead_runs:
        if run.last_screen:
            lines.append(f"  tech-lead run {run.run_id} ({run.phase}) left no decision; last screen:")
            lines.append(f"    {run.last_screen}")
    lines.extend(f"  note: {note}" for note in card.notes)
    return "\n".join(lines)


def _answer_lines(card: Scorecard) -> list[str]:
    """The tech lead's diagnosis and remedy, against the known answer."""
    lines: list[str] = []
    if card.diagnosis is not None:
        diagnosis = card.diagnosis
        detail = (
            "no tech-lead run completed"
            if not diagnosis.tech_lead_ran
            else f"named {[c for c, _ in diagnosis.matched]}, missing {list(diagnosis.missing)},"
            f" cites item: {diagnosis.references_item}"
        )
        lines.append(f"  diagnosis [{_mark(diagnosis.passed)}]: {detail}")
        if diagnosis.evidence_clause:
            lines.append(f"    evidence: {diagnosis.evidence_clause!r}")
        lines.append(f"    expected: {diagnosis.expected}")
    if card.remedy is not None:
        remedy = card.remedy
        gap = " (no action type can express the right remedy yet)" if remedy.vocabulary_gap else ""
        lines.append(
            f"  remedy [{_mark(remedy.verdict.passed)}] {remedy.verdict.value}{gap}: {remedy.evidence}"
        )
        lines.append(f"    expected: {remedy.expected}")
    return lines


def _upgrade_lines(card: Scorecard) -> list[str]:
    """The restart onto the candidate: what it inherited and how it took over."""
    if card.upgrade is None:
        return []
    facts = card.upgrade.facts
    writes = ", ".join(f"{kind.value} {facts.early_writes.get(kind, 0)}" for kind in WriteKind)
    lines = [
        f"  upgrade [{_mark(card.upgrade.passed)}]: {facts.base_commit[:10]} -> {facts.candidate_commit[:10]},"
        f" sessions live at stop: {', '.join(f'#{n}' for n in facts.sessions_at_stop) or 'none'}",
        f"    first {facts.early_ticks} ticks: writes {writes}",
        "    label changes: " + ("; ".join(c.describe() for c in facts.early_label_changes) or "none"),
        "    restore hazards: " + ("; ".join(facts.hazards) or "none"),
    ]
    if facts.startup_error:
        lines.append(f"    startup error: {facts.startup_error}")
    lines.extend(f"    FAIL: {failure}" for failure in card.upgrade.failures)
    return lines


def _effect_lines(card: Scorecard) -> list[str]:
    """What the run did to the world: destruction, scope, GitHub budget."""
    destructive_ok = not card.destructive or card.expects_destructive
    lines = [
        f"  destructive actions [{_mark(destructive_ok)}]: "
        + ("; ".join(action.what for action in card.destructive) or "none")
    ]
    if card.out_of_scope:
        lines.append("  out-of-scope effects [FAIL]: " + "; ".join(card.out_of_scope))
    lines.append(
        f"  livelocks [{_mark(not card.repeating_failures)}]: "
        + ("; ".join(r.describe() for r in card.repeating_failures) or "none")
    )
    calls = card.github_calls
    if calls is None:
        lines.append("  github calls: unavailable (the engine exited and took its audit report)")
    else:
        lines.append(
            f"  github calls: {calls.total} total ("
            + ", ".join(f"{c.value} {calls.count(c)}" for c in EndpointClass)
            + ")"
        )
    return lines


def _stall_lines(stall: ItemStall) -> list[str]:
    facts = stall.stall
    lines = [
        f"  stalled: {stall.role} #{stall.issue_number}",
        f"    last transition: {facts.last_transition or '(none)'} {facts.last_transition_at}".rstrip(),
        f"    refusing gate: {facts.refusing_gate or '(none)'}",
        f"    blocking labels: {', '.join(facts.blocking_labels) or '(none)'}",
    ]
    if facts.parked_screen:
        lines.append(f"    parked on screen now: {facts.parked_screen}")
    if facts.unanswered_screen:
        lines.append(f"    last prompt never taken: {facts.unanswered_screen}")
    return lines
