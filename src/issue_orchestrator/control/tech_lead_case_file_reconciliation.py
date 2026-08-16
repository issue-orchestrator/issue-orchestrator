"""Fold an ALREADY-accumulated duplicate cluster onto its case file (#6989).

``tech_lead_observation_routing`` stops the bleeding: from now on a cited
duplicate accrues to the durable case-file ledger instead of minting a fresh
open issue. It cannot retro-collapse what accumulated BEFORE it — and #6989
names that backlog explicitly (``#6966``/``#6977`` behind ``#6928``,
``#6959``/``#6970``/``#6973`` behind ``#6983``, ``#6961``/``#6978`` behind
``#6918``), plus the recurring classes that were never registered as
signatures at all.

That is a data operation, so this module is its **planner**, not its executor.
It turns a declarative, checked-in :class:`CaseFileReconciliationPlan` into the
SAME typed actions the live lane emits, and the ordinary ``ActionApplier``
executes them. Nothing here writes to GitHub, and no second policy for
"how evidence lands on a case file" exists.

Three properties make it safe to run against a live board:

* **Bounded.** It acts on exactly the issue numbers written in the plan file.
  It discovers nothing, scans nothing, and closes nothing it was not told
  about by name.
* **Idempotent.** Every write is create-once at a durable identity that is a
  pure function of the plan: the case file by its signature (the ledger row,
  plus :func:`~..domain.tech_lead_findings.case_file_issue_marker` remote
  recovery), each observation by
  :func:`~..domain.tech_lead_findings.pattern_observation_id` over
  ``(plan_id, session, action_id)``, and each closure by only being planned
  for a duplicate that is still open. Re-running a fully-applied plan plans
  the appends the ledger already recorded — which the owner then skips — and
  no closures at all.
* **Establishes nothing.** A reconciliation entry is an evidence-only
  :meth:`~.tech_lead_case_files.CaseFileIntake.sighting`, exactly like a live
  accrued duplicate: it carries no ``fix_class``, no ``area``, and no
  ``diagnosis``. Backfilled evidence must not decide whether a signature is
  promotable, which repo a promotion routes to, or what a promotion is filed
  on — those stay with a reviewed ``flag_pattern`` (#6989 round-1 review F1).
  A signature registered here is therefore a ledger + an evidence trail, and
  it stays UNCLASSIFIED until the tech lead diagnoses it.

Two phases, because the second depends on the first's result: a closure
comment names the case file the evidence moved to, and for a signature being
registered right now that issue number does not exist until phase 1 applies.
See :meth:`CaseFileReconciler.plan_ledger_actions` and
:meth:`CaseFileReconciler.plan_closure_actions`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Collection, Mapping, Protocol, Sequence

from ..domain.tech_lead_artifacts import ProposedTechLeadAction
from .actions import Action, CloseIssueAction
from .reconciliation import ExpectedState
from .tech_lead_case_files import CaseFileIntake, PatternCaseFilePlanner

if TYPE_CHECKING:
    from ..domain.tech_lead_findings import PatternEvidence
    from ..infra.config import Config
    from .action_results import ActionResult

#: Session name component of every observation identity this planner mints.
#: Constant on purpose — together with the plan's ``plan_id`` and each entry's
#: action id it makes the identity a pure function of the checked-in plan, so a
#: re-run reproduces it exactly and the store skips what it already recorded.
RECONCILIATION_SESSION_NAME = "case-file-reconciliation"

#: Action id of the entry that opens (or joins) a signature's case file.
REGISTRATION_ACTION_ID = "registration"


def _duplicate_action_id(issue_number: int) -> str:
    """Action id for the entry that folds one accumulated duplicate in."""
    return f"duplicate-{issue_number}"


@dataclass(frozen=True)
class AccumulatedDuplicate:
    """One already-open issue that is a re-sighting of a standing problem."""

    issue_number: int
    #: Why this issue is a re-sighting rather than distinct work. Written by
    #: whoever authored the plan and reproduced verbatim in the evidence the
    #: case file keeps, so the fold is reviewable after the issue is closed.
    note: str


@dataclass(frozen=True)
class StandingProblemCluster:
    """One recurring class: its signature, its live tracker, its duplicates."""

    signature: str
    #: The issue that owns the WORK for this class. It is never closed here —
    #: reconciliation moves the trailing evidence off the board and onto the
    #: ledger; it does not decide the tracker's fate.
    tracker_issue_number: int
    #: What the class is, in the plan author's words. Becomes the case file's
    #: first observation, so a signature with no accumulated duplicates is
    #: still a legitimate registration.
    summary: str
    duplicates: tuple[AccumulatedDuplicate, ...] = ()

    @property
    def duplicate_issue_numbers(self) -> tuple[int, ...]:
        return tuple(entry.issue_number for entry in self.duplicates)


@dataclass(frozen=True)
class CaseFileReconciliationPlan:
    """A checked-in, reviewable reconciliation: the whole input to this lane.

    ``plan_id`` is load-bearing, not a label: it is the first component of every
    observation identity this plan produces. Changing it re-posts every
    observation the plan already landed, so a published plan's id is immutable —
    a NEW backlog gets a NEW plan file.
    """

    plan_id: str
    clusters: tuple[StandingProblemCluster, ...]

    @classmethod
    def from_mapping(cls, data: Any) -> "CaseFileReconciliationPlan":
        """Parse and VALIDATE an operator-authored plan, or raise ``ValueError``.

        Strict on purpose. This plan closes issues by number, so a typo is a
        wrong issue closed on a live board: unknown keys are rejected rather
        than ignored, every number must be a positive int, and no issue may
        appear twice — as two duplicates, as a duplicate of two clusters, or as
        both a duplicate and a tracker. Fail-fast beats a partially-applied
        plan whose author believed something else happened.
        """
        mapping = _object(data, "plan")
        _reject_unknown_keys(mapping, {"plan_id", "clusters"}, "plan")
        plan_id = _non_empty_str(mapping, "plan_id", "plan")
        raw_clusters = mapping.get("clusters")
        if not isinstance(raw_clusters, list) or not raw_clusters:
            raise ValueError("plan 'clusters' must be a non-empty list")
        clusters = tuple(
            _cluster(entry, index) for index, entry in enumerate(raw_clusters)
        )
        _reject_collisions(clusters)
        return cls(plan_id=plan_id, clusters=clusters)

    @property
    def all_duplicate_issue_numbers(self) -> tuple[int, ...]:
        return tuple(
            number
            for cluster in self.clusters
            for number in cluster.duplicate_issue_numbers
        )


def _object(data: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be a mapping, got {type(data).__name__}")
    return data


def _reject_unknown_keys(
    mapping: Mapping[str, Any], known: set[str], where: str
) -> None:
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s) {unknown}; expected only {sorted(known)}"
        )


def _non_empty_str(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} '{key}' must be a non-empty string")
    return value.strip()


def _issue_number(mapping: Mapping[str, Any], key: str, where: str) -> int:
    value = mapping.get(key)
    # bool is an int subclass; an accidental `true` must not read as issue #1.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{where} '{key}' must be a positive issue number")
    return value


def _cluster(entry: Any, index: int) -> StandingProblemCluster:
    where = f"cluster #{index}"
    mapping = _object(entry, where)
    _reject_unknown_keys(
        mapping, {"signature", "tracker", "summary", "duplicates"}, where
    )
    signature = _non_empty_str(mapping, "signature", where)
    tracker = _issue_number(mapping, "tracker", where)
    summary = _non_empty_str(mapping, "summary", where)
    raw_duplicates = mapping.get("duplicates", [])
    if not isinstance(raw_duplicates, list):
        raise ValueError(f"{where} 'duplicates' must be a list")
    duplicates = tuple(
        _duplicate(item, f"{where} duplicate #{position}")
        for position, item in enumerate(raw_duplicates)
    )
    return StandingProblemCluster(
        signature=signature,
        tracker_issue_number=tracker,
        summary=summary,
        duplicates=duplicates,
    )


def _duplicate(entry: Any, where: str) -> AccumulatedDuplicate:
    mapping = _object(entry, where)
    _reject_unknown_keys(mapping, {"issue", "note"}, where)
    return AccumulatedDuplicate(
        issue_number=_issue_number(mapping, "issue", where),
        note=_non_empty_str(mapping, "note", where),
    )


def _reject_collisions(clusters: tuple[StandingProblemCluster, ...]) -> None:
    """No signature, and no issue number, may appear twice in one plan."""
    signatures: set[str] = set()
    for cluster in clusters:
        if cluster.signature in signatures:
            raise ValueError(
                f"signature {cluster.signature!r} appears in more than one"
                " cluster; one recurring class keeps exactly one case file"
            )
        signatures.add(cluster.signature)

    trackers = {cluster.tracker_issue_number for cluster in clusters}
    seen: set[int] = set()
    for cluster in clusters:
        for number in cluster.duplicate_issue_numbers:
            if number in trackers:
                raise ValueError(
                    f"issue #{number} is listed as a duplicate but is also a"
                    " cluster's tracker; reconciliation never closes a tracker"
                )
            if number in seen:
                raise ValueError(
                    f"issue #{number} is listed as a duplicate more than once;"
                    " an issue folds into exactly one case file"
                )
            seen.add(number)


class CaseFileReconciliationHost(Protocol):
    """The execution boundary one reconciliation run needs.

    Narrow on purpose: the run's POLICY (two phases, what may be closed, when to
    halt) belongs to :class:`CaseFileReconciler`, and this is only the three
    effects that policy cannot perform itself. The entrypoint binds it to the
    live orchestrator; a test binds it to a fake and gets the same policy.
    """

    def pattern_ledger(self) -> Mapping[str, "PatternEvidence"]:
        """The signature -> case-file ledger, re-read on each call."""
        ...

    def issue_is_open(self, issue_number: int) -> bool:
        """Is this named issue still open? One targeted read, never a scan."""
        ...

    def apply(self, actions: Sequence[Action]) -> Sequence["ActionResult"]:
        """Apply actions through the ordinary applier, in order."""
        ...


@dataclass(frozen=True)
class ReconciliationPhase:
    """What one phase planned, and what applying it did (empty when dry-run)."""

    name: str
    actions: tuple[Action, ...]
    results: tuple["ActionResult", ...] = ()

    @property
    def failures(self) -> tuple["ActionResult", ...]:
        return tuple(result for result in self.results if not result.success)

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class ReconciliationRun:
    """The outcome of one whole run, for the caller to render."""

    plan_id: str
    dry_run: bool
    evidence: ReconciliationPhase
    closure: ReconciliationPhase
    #: True when phase 1 failed, so no issue was closed. The distinction the
    #: operator needs: nothing was lost, and the run is safe to repeat.
    halted_before_closure: bool = False

    @property
    def ok(self) -> bool:
        return (
            not self.halted_before_closure
            and self.evidence.ok
            and self.closure.ok
        )


class CaseFileReconciler:
    """Owns one reconciliation run: its two phases and when to stop.

    Two phases, because a closure comment names the case file the evidence moved
    to and phase 1 is what creates it for a signature being registered now. The
    ledger is therefore re-read between them, so a case file created moments ago
    is closeable in the same run.
    """

    def __init__(self, *, config: "Config") -> None:
        self._config = config

    def run(
        self,
        plan: CaseFileReconciliationPlan,
        host: CaseFileReconciliationHost,
        *,
        observed_at: str,
        apply_writes: bool,
    ) -> ReconciliationRun:
        """Plan both phases, applying them only when ``apply_writes``.

        The halt rule is the safety property and lives here rather than in any
        caller: if phase 1 did not fully land, phase 2 does not run at all.
        Closing an issue whose evidence went nowhere would destroy the very
        record this lane exists to preserve.
        """
        evidence_actions = self.plan_ledger_actions(
            plan, pattern_ledger=host.pattern_ledger(), observed_at=observed_at
        )
        evidence = ReconciliationPhase(
            name="evidence",
            actions=tuple(evidence_actions),
            results=tuple(host.apply(evidence_actions)) if apply_writes else (),
        )
        if apply_writes and not evidence.ok:
            return ReconciliationRun(
                plan_id=plan.plan_id,
                dry_run=False,
                evidence=evidence,
                closure=ReconciliationPhase(name="closure", actions=()),
                halted_before_closure=True,
            )

        case_file_numbers = {
            row.signature: row.case_file_issue_number
            for row in host.pattern_ledger().values()
        }
        closure_actions = self.plan_closure_actions(
            plan,
            case_file_numbers=case_file_numbers,
            open_duplicates=self._open_duplicates(plan, host, case_file_numbers),
        )
        return ReconciliationRun(
            plan_id=plan.plan_id,
            dry_run=not apply_writes,
            evidence=evidence,
            closure=ReconciliationPhase(
                name="closure",
                actions=tuple(closure_actions),
                results=tuple(host.apply(closure_actions)) if apply_writes else (),
            ),
        )

    def _open_duplicates(
        self,
        plan: CaseFileReconciliationPlan,
        host: CaseFileReconciliationHost,
        case_file_numbers: Mapping[str, int],
    ) -> set[int]:
        """Which named duplicates are still open (GitHub API discipline).

        Bounded to the duplicates that could actually be closed this run — a
        cluster whose case file does not exist yet is skipped — so no read is
        spent on a closure that cannot be planned anyway.
        """
        return {
            number
            for cluster in plan.clusters
            if cluster.signature in case_file_numbers
            for number in cluster.duplicate_issue_numbers
            if host.issue_is_open(number)
        }

    def plan_ledger_actions(
        self,
        plan: CaseFileReconciliationPlan,
        *,
        pattern_ledger: Mapping[str, "PatternEvidence"],
        observed_at: str,
    ) -> list[Action]:
        """Phase 1 — register each signature and accrue its duplicates as evidence.

        Delegates every create-vs-append-vs-coalesce decision to
        :class:`~.tech_lead_case_files.PatternCaseFilePlanner`, the live lane's
        owner, so a backfill can neither open a second case file for a
        signature nor invent a second rendering of an observation.
        """
        actions: list[Action] = []
        for cluster in plan.clusters:
            planner = PatternCaseFilePlanner(
                config=self._config,
                actions=actions,
                # The tracker is what this evidence is reconciled against, so it
                # is the anchor the case file is derived from.
                anchor_issue_number=cluster.tracker_issue_number,
                pattern_ledger=pattern_ledger,
                findings={},
                source_run_id=plan.plan_id,
                source_session_name=RECONCILIATION_SESSION_NAME,
                observed_at=observed_at,
                expected=ExpectedState(),
            )
            planner.plan(self._registration_intake(cluster))
            for duplicate in cluster.duplicates:
                planner.plan(self._duplicate_intake(cluster, duplicate))
        return actions

    def plan_closure_actions(
        self,
        plan: CaseFileReconciliationPlan,
        *,
        case_file_numbers: Mapping[str, int],
        open_duplicates: Collection[int],
    ) -> list[Action]:
        """Phase 2 — close the duplicates whose evidence now lives on a case file.

        A duplicate is closed ONLY when both halves hold: its evidence actually
        landed (its signature has a case file in ``case_file_numbers``) and the
        issue is still open. So a phase-1 failure leaves the board untouched
        rather than closing issues whose evidence went nowhere, and a re-run
        after a completed one plans nothing.
        """
        still_open = set(open_duplicates)
        actions: list[Action] = []
        for cluster in plan.clusters:
            case_file = case_file_numbers.get(cluster.signature)
            if case_file is None:
                continue
            for duplicate in cluster.duplicates:
                if duplicate.issue_number not in still_open:
                    continue
                actions.append(
                    CloseIssueAction(
                        issue_number=duplicate.issue_number,
                        comment=_closure_comment(cluster, duplicate, case_file),
                        reason=(
                            f"#6989 reconciliation {plan.plan_id}: folded"
                            f" #{duplicate.issue_number} into case file"
                            f" #{case_file} for signature"
                            f" {cluster.signature!r}"
                        ),
                    )
                )
        return actions

    def _registration_intake(self, cluster: StandingProblemCluster) -> CaseFileIntake:
        return CaseFileIntake.sighting(
            ProposedTechLeadAction(
                id=REGISTRATION_ACTION_ID,
                # The evidence half of an intake is carried as a proposal
                # because that is the shape the case-file lane renders — the
                # same reuse ``tech_lead_observation_routing.case_file_sighting``
                # makes. It is not an agent proposal and is never executed as
                # one; only its body, id, and signature are read.
                action_type="flag_pattern",
                pattern_signature=cluster.signature,
                body=_registration_body(cluster),
            )
        )

    def _duplicate_intake(
        self, cluster: StandingProblemCluster, duplicate: AccumulatedDuplicate
    ) -> CaseFileIntake:
        return CaseFileIntake.sighting(
            ProposedTechLeadAction(
                id=_duplicate_action_id(duplicate.issue_number),
                action_type="flag_pattern",
                pattern_signature=cluster.signature,
                body=_duplicate_body(cluster, duplicate),
            )
        )


def _registration_body(cluster: StandingProblemCluster) -> str:
    lines = [
        cluster.summary,
        "",
        "---",
        "",
        "**Registered by case-file reconciliation (#6989).** This signature is"
        " the durable accrual point for a recurring class that was being"
        " re-filed as a new open issue per sighting. Work for the class is"
        f" tracked by #{cluster.tracker_issue_number}; this case file is its"
        " evidence ledger, so future sightings land here as comments instead"
        " of on the board.",
    ]
    if cluster.duplicates:
        lines.extend(
            [
                "",
                "Accumulated sightings folded in by this reconciliation:",
                *(
                    f"- #{duplicate.issue_number} — {duplicate.note}"
                    for duplicate in cluster.duplicates
                ),
            ]
        )
    lines.extend(
        [
            "",
            "Reconciliation establishes no classification and no diagnosis:"
            " backfilled evidence must not decide whether a signature is"
            " promotable or what a promotion would be filed on. Those stay"
            " with a reviewed `flag_pattern`.",
        ]
    )
    return "\n".join(lines)


def _duplicate_body(
    cluster: StandingProblemCluster, duplicate: AccumulatedDuplicate
) -> str:
    return "\n".join(
        [
            f"**Folded #{duplicate.issue_number} into this case file (#6989).**",
            "",
            duplicate.note,
            "",
            f"It was an accumulated re-sighting of the class tracked by"
            f" #{cluster.tracker_issue_number}, filed as its own open issue"
            " before cited duplicates routed to this ledger. The issue itself"
            " is closed by this reconciliation; its evidence lives here.",
        ]
    )


def _closure_comment(
    cluster: StandingProblemCluster,
    duplicate: AccumulatedDuplicate,
    case_file_issue_number: int,
) -> str:
    return (
        "## 🗂️ Folded into a pattern case file (#6989)\n\n"
        "This issue was a re-sighting of a standing problem that already had an"
        f" owner, so it is being closed as accumulated evidence rather than as"
        " work that was done.\n\n"
        f"- Work for this class is tracked by **#{cluster.tracker_issue_number}**.\n"
        f"- Its evidence now accrues to the pattern case file"
        f" **#{case_file_issue_number}** (signature `{cluster.signature}`),"
        " where this issue's contents were reproduced verbatim.\n\n"
        f"Why this was a duplicate: {duplicate.note}\n\n"
        "Reopen it if the cluster turns out to be a distinct problem."
    )
