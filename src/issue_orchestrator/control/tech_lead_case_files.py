"""Pattern case-file issues: the durable flag_pattern ledger (#6781).

``flag_pattern`` used to produce an event and a report line — observed
patterns evaporated unless someone read that session's report. Under execute
authority it now creates or appends to a **case-file issue** keyed by pattern
signature, so the tech-lead model gets an accumulating problem ledger the
operator can read on GitHub. This module is the single policy owner for the
case-file lifecycle, mirroring ``tech_lead_proposals`` (#6778) piece for piece:

* **Composition** — :func:`build_case_file_issue_action` turns the first
  observation of a signature into a :class:`CreateTechLeadCaseFileIssueAction`.
  The issue body is human documentation ONLY: dedup consults the ledger, so
  editing the issue after creation has zero effect (the tamper boundary).
* **Creation boundary** — the applier's single create-issue executor
  (``tech_lead_issue_creation.apply_create_tech_lead_issue``) records the
  ``(signature -> issue)`` ledger row create-once when it creates the issue.
* **Ledger dedup** — one case file per signature:
  :func:`build_pattern_ledger` projects the store's rows; a repeat
  observation plans an :class:`AddCommentAction` carrying the new evidence
  (:func:`build_case_file_evidence_comment`) instead of a second issue.
* **Classification** — :func:`split_tech_lead_case_file_issues` partitions the
  fact gatherer's ONE open-issue anchor scan (no extra GitHub call):
  observation-labeled issues become :class:`TechLeadCaseFileSummary` facts for
  the board snapshot and can never be mistaken for batch/health anchors.
  Startup recovery uses the same split so a case file is never requeued as
  an anchor.
* **Intake contract** — :class:`CaseFileIntake` is the ONE way an observation
  enters this lane. It carries the evidence (the proposal the lane renders and
  whose action id is the observation's durable identity) apart from the
  :class:`~..domain.tech_lead_findings.CaseFileClassification` the observation
  is entitled to establish, so "a reviewed ``flag_pattern`` diagnoses; an
  accrued duplicate sighting is evidence only" is stated once, at intake, and
  every builder below reads it from the same place (#6989 round-1 review
  F1/A1).
* **Per-decision planning** — :class:`PatternCaseFilePlanner` owns the whole
  create-vs-append-vs-coalesce decision for ONE tech-lead decision, plus the
  classification preflight that must run before any of them. That state
  (signature -> merged classification, signature -> the creation already
  planned this decision) exists only to serve case files, so it belongs here
  rather than mixed into the general decision planner's fields.
* **No terminal handling** — observations are not ops; there is nothing to
  execute or discard. Graduation is native: a firmed-up pattern gets a
  linked root-cause work issue, evidence trail intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable, TYPE_CHECKING, Iterable, Mapping, Sequence

from ..domain.tech_lead_findings import (
    CaseFileClassification,
    PatternEvidence,
    PatternObservation,
    case_file_issue_marker,
    pattern_observation_id,
    pattern_observation_marker,
)
from ..domain.tech_lead_session import (
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadCaseFileSummary,
    TechLeadCreationOrigin,
    is_tech_lead_observation_label,
    tech_lead_area_from_labels,
)
from .actions import (
    Action,
    ActionResult,
    AppendPatternObservationAction,
    CreateTechLeadCaseFileIssueAction,
)
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from .tech_lead_case_file_owner import PatternCaseFileOwner
from .tech_lead_issue_policy import case_file_issue_labels

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import ProposedTechLeadAction, TechLeadFinding
    from ..infra.config import Config
    from ..ports.pattern_registry import PatternCaseFileRegistry
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

CASE_FILE_TITLE_PREFIX = "Pattern case file: "


@dataclass(frozen=True)
class CaseFileIntake:
    """One observation entering the case-file lane: evidence + what it may claim.

    The lane composes an issue body, an evidence comment, and a durable
    observation identity from a :class:`ProposedTechLeadAction`, and it records
    a :class:`CaseFileClassification` on the ledger row. Those are two DIFFERENT
    provenances, and conflating them is what let an evidence-only sighting seed
    the canonical diagnosis of a signature that a later ``flag_pattern`` then
    made promotable (#6989 round-1 review F1).

    So the intake states both explicitly, and there are exactly two ways to
    build one:

    * :meth:`diagnosing` — a reviewed ``flag_pattern``. It classifies the
      signature and its body IS the canonical diagnosis a promotion is filed on.
    * :meth:`sighting` — an accrued duplicate re-sighting (#6989). Evidence
      only: it establishes nothing, so it can neither make a signature
      promotable, nor pick the repo a promotion routes to, nor become the
      diagnosis that promotion acts on. Its text still lands in the case file
      verbatim, which is the whole point of accruing it.

    Every builder and the planner take the intake, so a third caller cannot
    appear without saying which of the two it is. ``classification`` has no
    default for the same reason: an omitted one would silently drop a reviewed
    diagnosis, which is the exact defect this contract exists to prevent.
    """

    proposal: "ProposedTechLeadAction"
    classification: CaseFileClassification

    @classmethod
    def diagnosing(cls, proposed: "ProposedTechLeadAction") -> "CaseFileIntake":
        """A reviewed ``flag_pattern``: classifies AND diagnoses the signature."""
        return cls(
            proposal=proposed,
            classification=CaseFileClassification(
                fix_class=proposed.fix_class or "",
                area=proposed.area or "",
                diagnosis=proposed.body or "",
            ),
        )

    @classmethod
    def sighting(cls, proposed: "ProposedTechLeadAction") -> "CaseFileIntake":
        """An evidence-only observation: it establishes no durable fact."""
        return cls(proposal=proposed, classification=CaseFileClassification())

    @property
    def signature(self) -> str:
        """The ledger key this observation accrues under."""
        assert self.proposal.pattern_signature is not None  # enforced by validate()
        return self.proposal.pattern_signature


@dataclass(frozen=True)
class ResolvedCaseFileIntake:
    """An intake paired with the durable facts its signature resolved to.

    The lane needs BOTH values and they are deliberately different: the record
    it renders documents what THIS observation claimed
    (:attr:`claimed`), while the ledger row it writes carries the signature's
    reconciled facts (:attr:`durable`) — which may already hold a diagnosis or a
    classification this observation did not supply, because an earlier
    observation of the same signature did.

    Carrying them as one value is what makes the wrong pairing unrepresentable.
    Passing an intake and a separately-chosen classification would let a caller
    compose a creation from a diagnosing intake and an empty classification,
    silently dropping a reviewed diagnosis — F1 again, one layer down. The only
    producer is :meth:`PatternCaseFilePlanner.reconcile`, the preflight that
    does the reconciling, so every builder below is reached through it.
    """

    intake: CaseFileIntake
    durable: CaseFileClassification

    @property
    def proposal(self) -> "ProposedTechLeadAction":
        """The observation's evidence and its durable identity."""
        return self.intake.proposal

    @property
    def signature(self) -> str:
        """The ledger key this observation accrues under."""
        return self.intake.signature

    @property
    def claimed(self) -> CaseFileClassification:
        """What THIS observation asserted — what the rendered record shows."""
        return self.intake.classification


def build_pattern_ledger(
    evidence: Iterable[PatternEvidence],
) -> dict[str, PatternEvidence]:
    """Project the store's pattern rows to a signature -> evidence map.

    Rows are created with the case-file issue and never discarded — the
    case file IS the accumulating artifact — so this ledger enforces one
    case file per signature without a GitHub read.

    It carries the FULL durable row, not just the issue number: planning has to
    preflight a new observation's ``fix_class``/``area`` against what is already
    recorded, and it cannot do that from a number alone. Without it a
    conflicting classification was only discovered at apply time, AFTER the
    evidence comment had already been published (#6957 round-2 review F3).
    """
    return {row.signature: row for row in evidence}


def _evidence_lines(
    proposed: "ProposedTechLeadAction",
    findings: Mapping[str, "TechLeadFinding"],
) -> list[str]:
    """The observation's evidence block: linked findings + their refs."""
    lines: list[str] = []
    for finding_id in proposed.finding_ids:
        finding = findings.get(finding_id)
        if finding is None:
            continue
        lines.append(
            f"- **{finding_id}** ({finding.classification}): {finding.title}"
        )
        lines.extend(f"  - evidence: {ref}" for ref in finding.evidence)
    return lines


def _observation_body(
    resolved: ResolvedCaseFileIntake,
    *,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
) -> str:
    """One observation's record — shared by the issue body and comments.

    The classification columns render THIS observation's own claim, not the
    signature's merged row: the record documents what this sighting asserted,
    so an evidence-only one reads ``unclassified`` however the ledger is
    classified around it.
    """
    claimed = resolved.claimed
    fix_class = f"`fix:{claimed.fix_class}`" if claimed.fix_class else "unclassified"
    lines = [
        "| | |",
        "|---|---|",
        f"| Signature | `{resolved.signature}` |",
        f"| Area | {claimed.area or 'unclassified'} |",
        f"| Fix class | {fix_class} |",
        f"| Observed at | {observed_at} |",
        (
            f"| Observed by | session `{source_session_name}`"
            f" (run `{source_run_id}`, action {resolved.proposal.id}) |"
        ),
        f"| Anchor issue | #{anchor_issue_number} |",
        "",
        "### Observation",
        "",
        resolved.proposal.body or "",
    ]
    evidence = _evidence_lines(resolved.proposal, findings)
    if evidence:
        lines.extend(["", "### Evidence", "", *evidence])
    return "\n".join(lines)


def build_case_file_issue_action(
    resolved: ResolvedCaseFileIntake,
    *,
    config: "Config",
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    expected: "ExpectedState",
) -> CreateTechLeadCaseFileIssueAction:
    """Compose the case-file creation for a signature's FIRST observation.

    The ledger fields come from ``resolved.durable`` — the signature's MERGED
    row — never from the intake's raw claim: a second first-seen observation
    coalesces into this same action, so the durable fields must be able to carry
    a diagnosis or a class the creating observation did not itself supply
    (#6989 round-1 review F1).
    """
    signature = resolved.signature
    durable = resolved.durable
    # Deterministic remote provenance key. The case file is created on GitHub
    # BEFORE its ledger row is written, so a process that dies in between would
    # otherwise file a second case file for one signature on retry, splitting
    # the evidence promotion reads (#6957 round-2 review F10). The applier's
    # case-file owner recovers the existing issue by this marker instead.
    marker = case_file_issue_marker(signature)
    body = (
        f"## Pattern case file (#6781)\n\n"
        "A tech_lead session flagged a recurring cross-job pattern. This issue"
        " is its durable evidence ledger: every later observation of the"
        " same signature lands here as a comment, and comment cadence is"
        " the severity signal health reviews read from the board snapshot."
        "\n\n"
        + _observation_body(
            resolved,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
        )
        + f"\n\n> This is an orchestrator-owned observation ledger, keyed"
        f" orchestrator-side by its pattern signature when this issue was"
        f" created; editing this issue has no effect on that ledger. It is"
        f" never picked up as agent work (`{TECH_LEAD_OBSERVATION_LABEL}`)."
        " Graduation: link a root-cause work issue (or relabel into"
        " actionable work) when the pattern firms up."
        f"\n\n{marker}"
    )
    return CreateTechLeadCaseFileIssueAction(
        title=f"{CASE_FILE_TITLE_PREFIX}{signature}",
        body=body,
        labels=case_file_issue_labels(config, area=durable.area or None),
        pr_count=0,
        pattern_signature=signature,
        # Retained, not just rendered into the body: the anchor is the issue
        # this creation's reconciliation gate reads before any write, and the
        # origin makes "derived, therefore guarded" a state the command can
        # actually represent (#6957 F3/A3, R2 F6/A6).
        origin=TechLeadCreationOrigin.derived_from_anchor(anchor_issue_number),
        area=durable.area or None,
        fix_class=durable.fix_class,
        diagnosis=durable.diagnosis,
        idempotency_marker=marker,
        observations=(
            build_pattern_observation(
                resolved,
                anchor_issue_number=anchor_issue_number,
                findings=findings,
                source_run_id=source_run_id,
                source_session_name=source_session_name,
                observed_at=observed_at,
            ),
        ),
        reason=(
            f"tech_lead decision action {resolved.proposal.id}: open pattern case"
            f" file for signature {signature!r} (#6781)"
        ),
        expected=expected,
    )


def build_pattern_observation(
    resolved: ResolvedCaseFileIntake,
    *,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
) -> PatternObservation:
    """One identified observation: its stable identity + its evidence comment.

    The identity is what makes the durable count create-once (#6957 review F1),
    and it is embedded in the comment so a duplicate posted by a crash-retry is
    recognizable as the SAME observation rather than fresh evidence.
    """
    observation_id = pattern_observation_id(
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        action_id=resolved.proposal.id,
    )
    return PatternObservation(
        observation_id=observation_id,
        comment=build_case_file_evidence_comment(
            resolved,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
            observation_id=observation_id,
        ),
    )


def build_case_file_evidence_comment(
    resolved: ResolvedCaseFileIntake,
    *,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    observation_id: str,
) -> str:
    """The evidence comment for a REPEAT observation of a known signature."""
    return (
        "## 📌 Pattern observed again\n\n"
        + _observation_body(
            resolved,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
        )
        + f"\n\n{pattern_observation_marker(observation_id)}"
    )


def build_append_observation_action(
    resolved: ResolvedCaseFileIntake,
    *,
    case_file_issue_number: int,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    expected: "ExpectedState",
) -> AppendPatternObservationAction:
    """Plan a REPEAT observation of a known signature (comment + count).

    The count is the promotion lane's ``min_evidence`` input (#6957), so the
    comment and the increment must be one action with one owner — a bare
    comment would leave the count derivable only from GitHub comment cadence,
    which humans also write to.

    ``resolved.durable`` is what the PLANNER already reconciled against the
    durable row (and against earlier observations in the same decision), not
    this intake's raw claim: a conflict has to reject the decision before any
    action exists, so what reaches the store here can only be an upgrade or a
    no-op (#6957 round-2 review F3). Its ``diagnosis`` is what lets the first
    genuine ``flag_pattern`` establish the canonical diagnosis of a signature
    whose case file an evidence-only sighting opened (#6989 round-1 review F1).
    """
    signature = resolved.signature
    return AppendPatternObservationAction(
        issue_number=case_file_issue_number,
        pattern_signature=signature,
        observation=build_pattern_observation(
            resolved,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
        ),
        fix_class=resolved.durable.fix_class,
        area=resolved.durable.area,
        diagnosis=resolved.durable.diagnosis,
        reason=(
            f"tech_lead decision action {resolved.proposal.id}: pattern"
            f" {signature!r} observed again; appending evidence"
            f" to case file #{case_file_issue_number} (#6781)"
        ),
        expected=expected,
    )


@dataclass
class PatternCaseFilePlanner:
    """Plans the DURABLE half of one executed ``flag_pattern`` decision.

    Extracted from the general decision planner, whose fields it was the only
    consumer of: the merged-classification map and the "already planned this
    decision" index are case-file bookkeeping, and keeping them beside the
    create/append/coalesce rules puts the whole per-decision case-file policy
    under one owner (#6957 round-6 final abstraction pass).

    It appends into the decision's shared ``actions`` list because coalescing
    REPLACES a creation already planned in it — the position matters, so the
    list is the collaboration, not a return value.
    """

    config: "Config"
    actions: list["Action"]
    anchor_issue_number: int
    # signature -> its FULL durable row: planning preflights a new observation's
    # classification against it, which a bare issue number cannot support.
    pattern_ledger: Mapping[str, PatternEvidence]
    findings: Mapping[str, "TechLeadFinding"]
    source_run_id: str
    source_session_name: str
    observed_at: str
    expected: "ExpectedState"
    # signature -> the durable facts every observation seen so far in THIS
    # decision reconciled to, seeded from the durable row. Two observations that
    # disagree conflict with each other, not just with what is recorded
    # (#6957 round-2 review F3).
    _classification: dict[str, CaseFileClassification] = field(default_factory=dict)
    # signature -> index in ``actions`` of the creation this decision planned.
    _planned: dict[str, int] = field(default_factory=dict)

    def plan(self, intake: CaseFileIntake) -> None:
        """Create, append to, or coalesce into this signature's case file."""
        signature = intake.signature
        # Preflight FIRST: a classification conflict must reject the decision
        # before this produces any mutating action (#6957 R2 F3). Its result is
        # also the only way to reach the builders below, so nothing can be
        # composed from an unreconciled classification.
        resolved = self.reconcile(intake)
        existing = self.pattern_ledger.get(signature)
        if existing is not None:
            # Comment AND durable count under one owner (#6957): the count is
            # what promotion's min_evidence reads, so it can never be left to
            # GitHub comment cadence. The action carries the MERGED
            # classification, so the store's own reconcile is an upgrade or a
            # no-op — never a conflict discovered mid-write.
            self.actions.append(
                build_append_observation_action(
                    resolved,
                    case_file_issue_number=existing.case_file_issue_number,
                    anchor_issue_number=self.anchor_issue_number,
                    findings=self.findings,
                    source_run_id=self.source_run_id,
                    source_session_name=self.source_session_name,
                    observed_at=self.observed_at,
                    expected=self.expected,
                )
            )
            return
        planned_index = self._planned.get(signature)
        if planned_index is not None:
            self._coalesce(planned_index, resolved)
            return
        self._planned[signature] = len(self.actions)
        self.actions.append(
            build_case_file_issue_action(
                resolved,
                config=self.config,
                anchor_issue_number=self.anchor_issue_number,
                findings=self.findings,
                source_run_id=self.source_run_id,
                source_session_name=self.source_session_name,
                observed_at=self.observed_at,
                expected=self.expected,
            )
        )

    def reconcile(self, intake: CaseFileIntake) -> ResolvedCaseFileIntake:
        """Pair the intake with its signature's merged facts, or raise on conflict.

        The classification PREFLIGHT (#6957 round-2 review F3). It reconciles
        this observation against everything already known about the signature —
        the DURABLE row first, then whatever earlier observations in this same
        decision merged into — using the one rule the store enforces.

        Running it before any action is produced is what makes a conflict
        externally invisible: the raise unwinds into the whole-decision
        rejection in ``plan_tech_lead_decision_actions``, so no evidence
        comment, surface action, or sibling mutation is ever applied.
        Reconciling only at apply time published the conflicting comment first
        and left the durable row disagreeing with it.

        The merge covers the canonical ``diagnosis`` too, so a ``flag_pattern``
        establishes it for a signature whose case file an evidence-only sighting
        opened earlier in the same decision (#6989 round-1 review F1).
        """
        signature = intake.signature
        merged = self._classification.get(signature)
        if merged is None:
            recorded = self.pattern_ledger.get(signature)
            merged = (
                recorded.classification
                if recorded is not None
                else CaseFileClassification()
            )
        merged = merged.merged_with(intake.classification, signature=signature)
        self._classification[signature] = merged
        return ResolvedCaseFileIntake(intake=intake, durable=merged)

    def _coalesce(
        self, planned_index: int, resolved: ResolvedCaseFileIntake
    ) -> None:
        """Fold a second first-seen observation into the pending creation.

        One case file per signature, so a second observation of a signature this
        decision is already creating rides the SAME action as an extra
        identified observation, carrying the durable facts the preflight already
        merged. Retaining only the first action's values silently lost an
        ``unclassified -> code`` upgrade (and an area that decides routing)
        whenever both observations arrived in one decision (#6957 review F3) —
        and, once evidence-only sightings could open a case file, left an
        accrued sighting's text standing as the canonical diagnosis of a
        signature a sibling ``flag_pattern`` actually diagnosed
        (#6989 round-1 review F1).
        """
        creation = self.actions[planned_index]
        assert isinstance(creation, CreateTechLeadCaseFileIssueAction)
        durable = resolved.durable
        self.actions[planned_index] = replace(
            creation,
            observations=(
                *creation.observations,
                build_pattern_observation(
                    resolved,
                    anchor_issue_number=self.anchor_issue_number,
                    findings=self.findings,
                    source_run_id=self.source_run_id,
                    source_session_name=self.source_session_name,
                    observed_at=self.observed_at,
                ),
            ),
            fix_class=durable.fix_class,
            area=durable.area or None,
            diagnosis=durable.diagnosis,
            # An upgraded area changes the case file's ``area:*`` tag, so the
            # labels are recomposed by their policy owner rather than left
            # describing the first observation only.
            labels=case_file_issue_labels(self.config, area=durable.area or None),
        )


def apply_append_pattern_observation(
    action: "Action",
    *,
    repository_host: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
    pattern_registry: "PatternCaseFileRegistry | None" = None,
    before_write: Callable[[], None],
) -> "ActionResult":
    """Post a repeat observation and count it create-once (#6781/#6957).

    Delegates the comment/count ordering to :class:`PatternCaseFileOwner`, the
    same owner the creation boundary uses, so the two paths cannot drift on
    "already recorded means do nothing; otherwise comment, then count".

    Classification conflicts never reach here: the planner preflights every
    observation against the durable row and rejects the whole decision instead
    (#6957 round-2 review F3). The store still enforces the rule as a last line
    of defence, which fails this action rather than reclassifying a signature.

    The applier supplies mutation authority for every publication and durable
    count. Receipt reads may observe a concurrent pause or claim change; the
    registry's initial check alone cannot authorize a later write.
    """
    assert isinstance(action, AppendPatternObservationAction)
    if repository_host is None or authority is None:
        return ActionResult.fail(
            action,
            "pattern observation append requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    assert action.observation is not None  # enforced by the action's __post_init__
    if pattern_registry is None:
        from .pattern_registry import LocalPatternCaseFileRegistry

        pattern_registry = LocalPatternCaseFileRegistry(
            authority, before_write=before_write
        )
    try:
        outcome = PatternCaseFileOwner(
            registry=pattern_registry,
            repository_host=repository_host,
            add_comment=repository_host.add_comment,
            before_write=before_write,
        ).append_observations(
            signature=action.pattern_signature,
            issue_number=action.issue_number,
            observations=(action.observation,),
            fix_class=action.fix_class,
            area=action.area,
            diagnosis=action.diagnosis,
        )
    except (ReconciliationRequired, ClaimLostError):
        raise
    except Exception as exc:
        logger.exception(
            "Failed to append pattern observation for signature %r",
            action.pattern_signature,
        )
        return ActionResult.fail(action, str(exc))
    if outcome.deduplicated:
        return ActionResult.ok(
            action, issue_number=action.issue_number, deduplicated=True
        )
    return ActionResult.ok(action, issue_number=action.issue_number)


def build_case_file_summary(issue: "Issue") -> TechLeadCaseFileSummary:
    """Project one observation-labeled scan issue onto the board facts.

    ``comment_count``/``updated_at`` ride the SAME list-issues payload the
    anchor scan already fetched (GitHub API discipline: zero extra calls).
    """
    return TechLeadCaseFileSummary(
        issue_number=issue.number,
        title=issue.title,
        comment_count=issue.comment_count,
        updated_at=issue.updated_at or "",
        area=tech_lead_area_from_labels(issue.labels),
    )


def split_tech_lead_case_file_issues(
    issues: Sequence["Issue"],
) -> tuple[list["Issue"], tuple[TechLeadCaseFileSummary, ...]]:
    """Partition the anchor scan into (non-case-file issues, case files).

    One pass over the fact gatherer's existing open-issue scan, run AFTER
    the gated-proposal split and BEFORE anchor classification — mirroring
    proposals, an observation-labeled issue can never be mistaken for a
    batch/health anchor, and startup recovery never requeues one.
    """
    remaining: list["Issue"] = []
    case_files: list[TechLeadCaseFileSummary] = []
    for issue in issues:
        if any(is_tech_lead_observation_label(label) for label in issue.labels):
            case_files.append(build_case_file_summary(issue))
            continue
        remaining.append(issue)
    return remaining, tuple(case_files)


def case_file_area_counts(
    case_files: Sequence[TechLeadCaseFileSummary],
) -> tuple[tuple[str, int], ...]:
    """Open case files grouped by area (#6781 amendment trailing-window fact).

    Sorted by count (desc) then area name so the projection is
    deterministic; the empty area groups as "unclassified".
    """
    counts: dict[str, int] = {}
    for case_file in case_files:
        area = case_file.area or "unclassified"
        counts[area] = counts.get(area, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
