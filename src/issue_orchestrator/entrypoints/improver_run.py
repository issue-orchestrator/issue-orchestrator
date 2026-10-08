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
from concurrent.futures import ThreadPoolExecutor
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
    FindingSupport,
    HeatConflictRecord,
    HeatRecord,
    ImproverRunRecord,
    RunOutcome,
    StallPointMove,
)
from ..contracts.improver_variant import ImproverVariant
from ..control.improver_effects import design_finding_key, finding_key, planned_effects
from ..domain.engine_activity import EngineRef
from ..domain.improver_champion import INVITATION_RATE, ChangeInvitation, invited
from ..domain.improver_findings_validation import (
    ImproverFindingsRejected,
    StagedEvidence,
    validate_findings,
)
from ..domain.improver_heats import AcceptedHeat, merge_heats
from ..execution.improver_effect_applier import ImproverEffects
from ..ports.improver import (
    HeatSpace,
    ImproverAgent,
    ImproverAgentResult,
    ImproverRunStore,
    heat_file,
)
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

    @property
    def hidden_from_toolbox(self) -> frozenset[int]:
        """The hidden issues the toolbox's GitHub reads could otherwise reach:
        those of the audited repository, when it is also the outputs one
        (#8001 r5 F1)."""
        return self.excluded_open_issues if self.engine.repo == self.outputs_repo else frozenset()

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


#: In the run dir: each heat's own workdir, ``heats/h<k>/``.
HEATS_DIRNAME = "heats"
#: Each heat is a whole agent run; more than this is not a modest default
#: but a tournament, which belongs to the tournament harness.
MAX_HEATS = 5


@dataclass(frozen=True)
class HeatPlan:
    """How many heats a run sends, and how many run at once (#8001).

    Each heat is a whole agent run on the provider (a Claude heat counts
    against the operator's subscription), so the CLI's default is modest."""

    count: int
    parallel: int

    def __post_init__(self) -> None:
        if not 1 <= self.count <= MAX_HEATS:
            raise ValueError(f"a run sends 1 to {MAX_HEATS} heats, not {self.count}")
        if not 1 <= self.parallel <= self.count:
            raise ValueError(f"1 to {self.count} heats run at once, not {self.parallel}")

    @property
    def waves(self) -> int:
        """How many heats run one after another at most: the run takes up to
        this many agent timeouts."""
        return -(-self.count // self.parallel)

    def require_within(self, *, agent_timeout_minutes: int, budget_minutes: int) -> None:
        """Raise unless every wave of heats, each up to the agent's timeout,
        fits the run's budget (queued heats wait for a wave to finish)."""
        if self.waves * agent_timeout_minutes > budget_minutes:
            raise ValueError(
                f"{self.waves} wave(s) of heats x {agent_timeout_minutes} minutes exceeds the"
                f" {budget_minutes}-minute run budget; run more heats at once, or fewer"
            )


@dataclass(frozen=True)
class ChangePolicy:
    """Runs of the champion are sometimes invited to propose one change to
    it (#8001): about ``rate`` of them, chosen by run id, never by the agent.
    ``addendum`` is the invitation appended to the prompt (``<<CHAMPION>>``
    names the champion)."""

    champion: ImproverVariant
    prompt: str
    addendum: str
    rate: float = INVITATION_RATE

    def invitation(self, run_id: str) -> ChangeInvitation | None:
        return ChangeInvitation(self.champion, self.prompt) if invited(run_id, rate=self.rate) else None

    def instructions(self) -> str:
        return "\n\n" + self.addendum.replace("<<CHAMPION>>", f"`{self.champion.id}` ({self.champion.describe()})")


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
        heats: HeatPlan,
        clock: Callable[[], datetime],
        change_policy: ChangePolicy | None = None,
    ) -> None:
        if change_policy is not None:
            # Only the champion itself is invited to propose a change to it.
            champion = change_policy.champion
            mismatched = [
                what for what, ok in (
                    ("prompt", prompt == change_policy.prompt),
                    ("agent", agent.choice == champion.agent),
                    ("mode", investigation.mode is champion.mode),
                    ("heats", heats.count == champion.heats),
                ) if not ok
            ]
            if mismatched:
                raise ValueError(f"a run invited to change the champion runs the champion; its {mismatched} differ")
        self._change_policy = change_policy
        self._store = store
        self._heats = heats
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
        # A blind run files nothing, so it is never invited to propose a change.
        invitation = None if request.blind or self._change_policy is None else self._change_policy.invitation(run_id)
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
            change_invitation=None if invitation is None else invitation.champion.id,
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
        answers = self._investigate(request, run_dir, base, invited=invitation is not None)
        if isinstance(answers, ImproverRunRecord):
            return answers
        heats = [
            self._judge(heat, answer, run_dir, evidence, request.engine, invitation) for heat, answer in answers
        ]
        records = tuple(record for record, _ in heats)
        base = base.model_copy(update={"heats": records})
        accepted = [AcceptedHeat(record.heat, findings) for record, findings in heats if findings is not None]
        if not accepted:
            return self._finish_unaccepted(base, records)
        merged = merge_heats(
            accepted,
            lambda f: finding_key(f, request.engine),
            lambda d: design_finding_key(d, request.engine),
        )
        text = merged.findings.model_dump_json(indent=2, by_alias=True) + "\n"
        (run_dir / FINDINGS_FILE).write_text(text, encoding="utf-8")
        try:
            findings = validate_findings(text, evidence, invitation=invitation)
        except ImproverFindingsRejected as rejection:
            # Each heat was accepted alone: a rejected merge is a merge defect.
            return self._finish(
                base,
                RunOutcome.REJECTED,
                f"the merge of {len(accepted)} accepted heat(s) broke {len(rejection.violations)} rule(s);"
                " nothing applied",
                rejections=tuple(v.describe() for v in rejection.violations),
            )
        accepted_run = self._finish(
            base,
            RunOutcome.ACCEPTED,
            f"{len(findings.findings)} finding(s) and {len(findings.design_findings)} design finding(s) accepted"
            f" from {len(accepted)} of {len(records)} heat(s)"
            + ("; blind run: nothing is filed" if request.blind else ""),
            finding_support=tuple(
                FindingSupport(finding_id=finding_id, heats=heats_seen)
                for finding_id, heats_seen in sorted(merged.support.items())
            ),
            heat_conflicts=tuple(
                HeatConflictRecord(finding_id=c.finding_id, heat=c.heat, reason=c.reason, claim=c.claim)
                for c in merged.conflicts
            ),
            grades=_grades(findings),
            stall_points=self._stall_point_moves(findings, request.engine.engine_id),
            trend=findings.trend,
            # A blind run's findings may duplicate the issues it was not shown.
            effects=() if request.blind else planned_effects(
                findings, request.engine, merged.original_ids,
                change_base=None if invitation is None else invitation.champion.id,
            ),
        )
        if not apply:
            return accepted_run
        self._effects.apply_pending()
        current = self._explain_unapplied(next(r for r in self._store.runs() if r.run_id == accepted_run.run_id))
        earlier = tuple(r for r in self._effects.owing_runs() if r != current.run_id)
        if not earlier:
            return current
        owed = current.model_copy(update={"owed_by_earlier_runs": earlier})
        self._store.record(owed)
        return owed

    def _investigate(
        self, request: ImproverRunRequest, run_dir: Path, base: ImproverRunRecord, *, invited: bool
    ) -> list[tuple[int, ImproverAgentResult]] | ImproverRunRecord:
        """Each heat's answer, run inside the investigation (the toolbox is
        staged once and served while the heats run), or the finished record
        of a run whose toolbox could not even start."""
        with ExitStack() as investigation:
            try:
                kit = investigation.enter_context(
                    self._investigation.open(request.engine, run_dir, hidden_issues=request.hidden_from_toolbox)
                )
            except Exception as error:
                # The toolbox could not be staged or served: no agent ran.
                return self._finish(
                    base, RunOutcome.UNAVAILABLE, f"toolbox unavailable: {type(error).__name__}: {error}"
                )
            root = run_dir.resolve()
            invitation = self._change_policy.instructions() if invited and self._change_policy is not None else ""
            prompt = f"ISSUE_ORCHESTRATOR_RUN_DIR={root}\n\n{self._prompt}{kit.instructions}{invitation}"
            evidence = ((root / IMPROVER_DATA_DIRNAME), *kit.evidence)

            def heat_answer(heat: int) -> tuple[int, ImproverAgentResult]:
                # Its own workdir: no heat can read another's answer, so
                # the heats that find a finding are independent support.
                workdir = root / HEATS_DIRNAME / f"h{heat}"
                workdir.mkdir(parents=True)
                space = HeatSpace(heat=heat, run_dir=root, workdir=workdir, evidence=evidence)
                try:
                    return heat, self._agent.run(prompt=prompt, space=space, toolbox=kit.toolbox)
                except Exception as error:
                    # The agent could not even be launched (an incompatible
                    # Codex config, a missing binary): the heat failed, with why.
                    return heat, ImproverAgentResult(None, f"agent not launched: {type(error).__name__}: {error}")

            with ThreadPoolExecutor(max_workers=self._heats.parallel, thread_name_prefix="improver-heat") as pool:
                return list(pool.map(heat_answer, range(1, self._heats.count + 1)))

    def _judge(
        self,
        heat: int,
        answer: ImproverAgentResult,
        run_dir: Path,
        evidence: StagedEvidence,
        engine: EngineRef,
        invitation: ChangeInvitation | None,
    ) -> tuple[HeatRecord, ImproverFindings | None]:
        """One heat's answer, validated alone; its findings if accepted."""
        if answer.final_message is None:
            return HeatRecord(heat=heat, outcome=RunOutcome.AGENT_FAILED, detail=answer.detail), None
        text = findings_text(answer.final_message)
        (run_dir / heat_file(FINDINGS_FILE, heat)).write_text(text, encoding="utf-8")
        try:
            findings = validate_findings(text, evidence, invitation=invitation)
        except ImproverFindingsRejected as rejection:
            return HeatRecord(
                heat=heat,
                outcome=RunOutcome.REJECTED,
                detail=f"{len(rejection.violations)} rule violation(s)",
                rejections=tuple(v.describe() for v in rejection.violations),
            ), None
        duplicates = self._duplicate_effect_keys(findings, engine)
        if duplicates:
            return HeatRecord(
                heat=heat,
                outcome=RunOutcome.REJECTED,
                detail=f"{len(duplicates)} effect key(s) claimed by two findings",
                rejections=duplicates,
            ), None
        return HeatRecord(
            heat=heat,
            outcome=RunOutcome.ACCEPTED,
            detail="accepted",
            findings=len(findings.findings),
            design_findings=len(findings.design_findings),
        ), findings

    @staticmethod
    def _duplicate_effect_keys(findings: ImproverFindings, engine: EngineRef) -> tuple[str, ...]:
        """Two findings of one answer with the same effect key ask for the
        same GitHub effect: they are one finding named twice, and the one
        identity the merge and the effects share would not hold."""
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        keyed = [(finding_key(f, engine), f.id) for f in findings.findings] + [
            (design_finding_key(d, engine), d.id) for d in findings.design_findings
        ]
        for key, finding_id in keyed:
            if key in seen:
                duplicates.append(
                    f"[unique_effect_keys] finding {finding_id}: asks for the same effect as {seen[key]}"
                    f" (key {key}); report it once"
                )
            seen.setdefault(key, finding_id)
        return tuple(duplicates)

    def _finish_unaccepted(self, base: ImproverRunRecord, heats: tuple[HeatRecord, ...]) -> ImproverRunRecord:
        """No heat was accepted: rejected if any answered and broke a rule
        (their reasons kept), else failed with each heat's reason."""
        rejected = [h for h in heats if h.outcome is RunOutcome.REJECTED]
        if rejected:
            return self._finish(
                base,
                RunOutcome.REJECTED,
                f"all {len(heats)} heat(s) unaccepted; {len(rejected)} broke a rule; nothing applied",
                rejections=tuple(f"heat {h.heat}: {r}" for h in rejected for r in h.rejections),
            )
        return self._finish(
            base, RunOutcome.AGENT_FAILED, "; ".join(f"heat {h.heat}: {h.detail}" for h in heats)
        )

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
    lines += [
        f"  heat {h.heat}: {h.outcome.value}: {h.detail}"
        + (f" ({h.findings} finding(s), {h.design_findings} design)" if h.outcome is RunOutcome.ACCEPTED else "")
        for h in record.heats
    ]
    lines += [
        f"  {s.finding_id}: found by {len(s.heats)} of {len(record.heats)} heat(s)" for s in record.finding_support
    ]
    lines += [
        f"  {c.finding_id}: heat {c.heat} not merged: {c.reason}" for c in record.heat_conflicts
    ]
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


__all__ = ["MAX_HEATS", "HeatPlan", "ImproverRun", "ImproverRunRequest", "findings_text", "render_run"]
