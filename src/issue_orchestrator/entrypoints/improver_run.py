"""One run of the tech-lead improver, start to finish (#7490).

The owner of the run's sequence, which is also its trust boundary:

1. apply whatever an earlier accepted run still owes GitHub;
2. stage the inputs (:mod:`.improver_staging`); an input the improver cannot
   run without ends the run ``unavailable``;
3. run the agent read-only on the prompt, inside its investigation (the
   staged bundle alone, or, EMPOWERED, with the read-only toolbox staged and
   served for the agent's run, #8001); its final message is its output;
4. validate that output strictly against what was staged; a rejection is
   recorded with every broken rule and NOTHING is applied;
5. record the accepted run (its stall-point grades and how they moved since
   the previous accepted run), then apply its effects through
   :class:`~..control.improver_effects.ImproverEffects`.

Every step's outcome lands in the run store, so ``improver status`` and the
next run read the same history.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from ..contracts.improver_findings import FINDINGS_FILE, ImproverFindings
from ..contracts.improver_inputs import (
    AUDIT_FILE,
    EXAM_DIRNAME,
    IMPROVER_DATA_DIRNAME,
    PREVIOUS_SCORECARD_SUFFIX,
    ScorecardHead,
)
from ..contracts.improver_run import (
    ExamScore,
    FindingGrade,
    ImproverRunRecord,
    RunOutcome,
    StallPointMove,
)
from ..control.improver_effects import planned_effects
from ..execution.improver_effect_applier import ImproverEffects
from ..domain.engine_activity import EngineRef
from ..domain.improver_findings_validation import (
    ImproverFindingsRejected,
    StagedEvidence,
    validate_findings,
)
from ..ports.improver import ImproverAgent, ImproverRunStore
from ..ports.improver_investigation import ImproverInvestigation
from .improver_staging import (
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    StagedImproverInputs,
    load_staged_evidence,
)


class ImproverInputStaging(Protocol):
    """The staging owner (:class:`.improver_staging.ImproverInputStager`)."""

    def stage(self, request: ImproverStagingRequest) -> StagedImproverInputs: ...

_FENCED = re.compile(r"\A```(?:json)?\n(?P<body>.*)\n```\Z", re.DOTALL)


@dataclass(frozen=True)
class ImproverRunRequest:
    """What one run audits (one engine) and where its outputs go."""

    engine: EngineRef
    outputs_repo: str
    exam_dir: Path | None
    window: timedelta
    log_tail_bytes: int
    #: A BLIND run: these open issues are hidden from the improver, and the
    #: run files nothing (see ``ImproverRunRecord.blind_excluded_issues``).
    excluded_open_issues: frozenset[int] = frozenset()

    @property
    def blind(self) -> bool:
        return bool(self.excluded_open_issues)

    def staging(self, run_dir: Path, previous_audit: Path | None) -> ImproverStagingRequest:
        return ImproverStagingRequest(
            engine=self.engine,
            outputs_repo=self.outputs_repo,
            run_dir=run_dir,
            previous_audit=previous_audit,
            exam_dir=self.exam_dir,
            window=self.window,
            log_tail_bytes=self.log_tail_bytes,
            excluded_open_issues=self.excluded_open_issues,
        )


class ImproverRun:
    def __init__(
        self,
        *,
        store: ImproverRunStore,
        stager: ImproverInputStaging,
        agent: ImproverAgent,
        investigation: ImproverInvestigation,
        effects: ImproverEffects,
        prompt: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._stager = stager
        self._agent = agent
        self._investigation = investigation
        self._effects = effects
        self._prompt = prompt
        self._clock = clock

    def run(self, request: ImproverRunRequest, *, apply: bool = True) -> ImproverRunRecord:
        """One run, holding the store throughout (raises
        :class:`~..ports.improver.ImproverStoreBusy` if another holds it).
        ``apply=False`` records an accepted run's effects as owed without
        touching GitHub (``improver apply`` applies them later)."""
        with self._store.exclusive():
            return self._run(request, apply=apply)

    def _run(self, request: ImproverRunRequest, *, apply: bool) -> ImproverRunRecord:
        if request.blind and apply:
            raise ValueError("a blind run (hidden open issues) never applies: its findings would duplicate them")
        if request.outputs_repo != self._effects.outputs_repo:
            raise ValueError(
                f"the run files into {request.outputs_repo} but its effects apply to"
                f" {self._effects.outputs_repo}"
            )
        if apply:
            self._effects.apply_pending()
        started = self._clock()
        run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        run_dir = self._store.new_run_dir(run_id)
        base = ImproverRunRecord(
            run_id=run_id,
            started_at=started,
            finished_at=started,
            outcome=RunOutcome.UNAVAILABLE,
            detail="",
            engine_id=request.engine.engine_id,
            audited_repo=request.engine.repo,
            outputs_repo=request.outputs_repo,
            run_dir=str(run_dir),
            blind_excluded_issues=tuple(sorted(request.excluded_open_issues)),
            agent=self._agent.choice,
            mode=self._investigation.mode,
        )
        try:
            staged = self._stager.stage(
                request.staging(run_dir, self._previous_audit(request.engine.engine_id))
            )
        except ImproverInputsUnavailable as error:
            return self._finish(base, RunOutcome.UNAVAILABLE, f"inputs unavailable: {error}")
        evidence = load_staged_evidence(staged.data_dir)
        base = base.model_copy(
            update={
                "engine_commit": evidence.engine_start.engine_commit,
                "audit_staged": True,
                "exam_scores": _exam_scores(evidence),
            }
        )
        with ExitStack() as investigation:
            try:
                kit = investigation.enter_context(self._investigation.open(request.engine, run_dir))
            except Exception as error:
                # The toolbox could not be staged or served: no agent ran.
                return self._finish(
                    base, RunOutcome.UNAVAILABLE, f"toolbox unavailable: {type(error).__name__}: {error}"
                )
            try:
                answer = self._agent.run(
                    prompt=f"ISSUE_ORCHESTRATOR_RUN_DIR={run_dir}\n\n{self._prompt}{kit.instructions}",
                    run_dir=run_dir,
                    toolbox=kit.toolbox,
                )
            except Exception as error:
                # The agent could not even be launched (an incompatible Codex
                # config refused by the sandbox profile, a missing binary): the
                # run is recorded unavailable, never lost without a record.
                return self._finish(
                    base, RunOutcome.AGENT_FAILED, f"agent not launched: {type(error).__name__}: {error}"
                )
        if answer.final_message is None:
            return self._finish(base, RunOutcome.AGENT_FAILED, answer.detail)
        text = findings_text(answer.final_message)
        (run_dir / FINDINGS_FILE).write_text(text, encoding="utf-8")
        try:
            findings = validate_findings(text, evidence)
        except ImproverFindingsRejected as rejection:
            return self._finish(
                base,
                RunOutcome.REJECTED,
                f"{len(rejection.violations)} rule violation(s); nothing applied",
                rejections=tuple(v.describe() for v in rejection.violations),
            )
        accepted = self._finish(
            base,
            RunOutcome.ACCEPTED,
            f"{len(findings.findings)} finding(s) accepted"
            + ("; blind run: nothing is filed" if request.blind else ""),
            grades=_grades(findings),
            stall_points=self._stall_point_moves(findings, request.engine.engine_id),
            trend=findings.trend,
            # A blind run's findings may duplicate the issues it was not shown.
            effects=() if request.blind else planned_effects(findings, request.engine),
        )
        if not apply:
            return accepted
        self._effects.apply_pending()
        current = self._explain_unapplied(next(r for r in self._store.runs() if r.run_id == accepted.run_id))
        earlier = tuple(r for r in self._effects.owing_runs() if r != current.run_id)
        if not earlier:
            return current
        owed = current.model_copy(update={"owed_by_earlier_runs": earlier})
        self._store.record(owed)
        return owed

    def _explain_unapplied(self, run: ImproverRunRecord) -> ImproverRunRecord:
        """Say why effects left pending without a reason of their own were
        not tried: an earlier run's effect stopped the batch first."""
        silent = [e for e in run.pending_effects if e.error is None and not e.detail]
        if not silent:
            return run
        explained = run.model_copy(
            update={
                "effects": tuple(
                    e.model_copy(update={"detail": "not tried: an earlier run's effect stopped the batch"})
                    if e in silent
                    else e
                    for e in run.effects
                )
            }
        )
        self._store.record(explained)
        return explained

    def _finish(self, base: ImproverRunRecord, outcome: RunOutcome, detail: str, **fields: object) -> ImproverRunRecord:
        record = base.model_copy(
            update={"outcome": outcome, "detail": detail, "finished_at": self._clock(), **fields}
        )
        self._store.record(record)
        return record

    def _previous_audit(self, engine_id: str) -> Path | None:
        """The latest staged audit of the SAME engine."""
        for run in self._store.runs():
            if run.audit_staged and run.engine_id == engine_id:
                return Path(run.run_dir) / IMPROVER_DATA_DIRNAME / AUDIT_FILE
        return None

    def _stall_point_moves(self, findings: ImproverFindings, engine_id: str) -> tuple[StallPointMove, ...]:
        """How the grades moved since the previous audit of the engine BY THE
        SAME AGENT IN THE SAME MODE: a change of provider, model or mode is
        not a change in the engine, so it starts a new baseline (#8001)."""
        current = Counter(f.stall_point for f in findings.findings)
        previous_run = next(
            (
                r
                for r in self._store.runs()
                if r.is_engine_audit
                and r.engine_id == engine_id
                and r.agent == self._agent.choice
                and r.mode == self._investigation.mode
            ),
            None,
        )
        previous = None if previous_run is None else Counter(g.stall_point for g in previous_run.grades)
        points = sorted(set(current) | set(previous or ()))
        return tuple(
            StallPointMove(
                stall_point=point,
                previous=None if previous is None else previous[point],
                current=current[point],
            )
            for point in points
        )


def render_run(record: ImproverRunRecord) -> str:
    """One run for an operator: how it ended, its grades and their trend, its effects."""
    lines = [
        f"{record.run_id} {record.outcome.value}: {record.detail}",
        f"  audited {record.audited_repo} (engine {record.engine_id} at {record.engine_commit});"
        f" outputs to {record.outputs_repo}",
        f"  agent {record.agent.describe() if record.agent else 'codex (recorded before #8001)'},"
        f" {record.mode.value if record.mode else 'scripted'}",
        f"  run dir {record.run_dir}",
    ]
    if record.blind_excluded_issues:
        lines.append(
            "  blind: hid " + ", ".join(f"#{n}" for n in record.blind_excluded_issues) + "; files nothing"
        )
    lines += [f"  rejected: {reason}" for reason in record.rejections]
    lines += [
        f"  {g.finding_id}: stalled at {g.stall_point} -> {g.output} ({g.classification})"
        for g in record.grades
    ]
    lines += [
        f"  stall point {m.stall_point}: {'-' if m.previous is None else m.previous} -> {m.current}"
        for m in record.stall_points
    ]
    if record.trend is not None:
        lines.append(
            f"  trend: exam scores {record.trend.exam_scores},"
            f" interventions {record.trend.operator_interventions}"
        )
    lines += [
        f"  effect {e.finding_id}: {e.status.value}"
        + (f" #{e.issue_number}" if e.issue_number else "")
        + (f" ({e.detail})" if e.detail else "")
        + (f" error: {e.error}" if e.error else "")
        for e in record.effects
    ]
    if record.owed_by_earlier_runs:
        lines.append(f"  still owed by earlier runs: {', '.join(record.owed_by_earlier_runs)}")
    return "\n".join(lines)


def findings_text(message: str) -> str:
    """The findings document in the agent's final message: the message itself,
    or the one fenced block that is all of it."""
    stripped = message.strip()
    fenced = _FENCED.match(stripped)
    return (fenced.group("body") if fenced else stripped) + "\n"


def _grades(findings: ImproverFindings) -> tuple[FindingGrade, ...]:
    return tuple(
        FindingGrade(
            finding_id=f.id,
            anomaly_keys=f.anomaly_keys,
            classification=f.classification,
            stall_point=f.stall_point,
            output=f.output,
        )
        for f in findings.findings
    )


def _exam_scores(evidence: StagedEvidence) -> tuple[ExamScore, ...]:
    """The latest scorecard of each case staged, as case and verdict."""
    cards = (
        ScorecardHead.model_validate(doc)
        for name, doc in sorted(evidence.documents.items())
        if name.startswith(f"{EXAM_DIRNAME}/") and not name.endswith(PREVIOUS_SCORECARD_SUFFIX)
    )
    return tuple(ExamScore(case_id=card.case_id, passed=card.passed) for card in cards)


__all__ = ["ImproverRun", "ImproverRunRequest", "findings_text", "render_run"]
