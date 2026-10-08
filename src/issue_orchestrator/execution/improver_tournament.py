"""The improver tournament, productized (#8001).

The 2026-10-04 tournament (``run_heat.sh``, a hand anonymizer, two graders)
as one tested owner, :class:`TournamentHarness`:

1. **Arms on a frozen snapshot.** Each arm (a provider, model and mode) is an
   ordinary :class:`~..entrypoints.improver_run.ImproverRun` whose inputs and
   toolbox are copied from the snapshot (:class:`FrozenInputs`,
   :class:`FrozenToolbox`), never staged live, so every arm, however much
   later it runs, reads the same evidence. Its heats are the arm's outputs.
   A tournament never touches GitHub: its runs never apply, their effects
   host refuses every call, and a frozen run reads no live GitHub (today's
   GitHub would leak what was found after the snapshot).
2. **Anonymized.** The outputs are relabelled ``S10``..``S99`` in a seeded
   random order; the mapping is sealed in ``sealed/``, which no grader can
   read.
3. **Cross-model graders.** Each grader (default: one Claude, one Codex) is
   a read-only agent that reads only ``anon/`` and ``key/`` and must grade
   every output on every scored key item (anything less is no grade).
4. **Scored and ranked** by :mod:`..domain.improver_tournament`. A heat that
   produced no output scores 0 for every grader.

Everything a tournament writes stays in its own directory under
``<store>/tournaments/<id>/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..contracts.improver_findings import FINDINGS_FILE
from ..contracts.improver_inputs import INPUTS_FILE, InputsManifest
from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider, RunOutcome
from ..contracts.improver_toolbox import ToolboxManifest
from ..contracts.improver_tournament import (
    GRADE_CREDIT,
    AnswerKey,
    ArmComparison,
    ArmScore,
    GraderRun,
    TournamentArm,
    TournamentCost,
    TournamentNoise,
    TournamentResult,
    require_slug,
)
from ..domain.engine_activity import EngineRef
from ..domain.improver_tournament import (
    ALPHA,
    NOISE_BAND_SES,
    GradesRejected,
    PooledScores,
    anonymize,
    finding_ids,
    pool,
    rank,
    read_grades,
    score,
)
from ..entrypoints.improver_run import (
    HeatPlan,
    ImproverRun,
    ImproverRunRequest,
    findings_text,
)
from ..entrypoints.improver_staging import ImproverStagingRequest, StagedImproverInputs
from ..ports.improver import HeatSpace, ImproverAgent, heat_file
from .improver_answer_keys import FileAnswerKeyStore
from .improver_effect_applier import ImproverEffects
from .improver_investigation import EmpoweredInvestigation, ScriptedInvestigation
from .improver_run_store import FileImproverRunStore
from .improver_snapshots import FrozenSnapshotStore, audit_of

TOURNAMENTS_DIRNAME = "tournaments"
#: A tournament's outputs are filed nowhere: this names its effects' repository.
NO_OUTPUTS_REPO = "tournament/no-outputs"
#: Each grader grades every output this many times; the spread is the noise.
DEFAULT_PASSES = 3
#: Each grading's record (who, which pass, accepted, seconds), beside its answer.
_GRADING_RECORD = "grading.json"
#: What was graded (outputs, seed, snapshot), sealed with the mapping, so a failed grading can be retried.
_REQUEST = "sealed/request.json"
#: What the tournament's arms were run on (written by run_arms).
_ARMS_RUN = "arms/run.json"


class FrozenInputs:
    """An arm's staged inputs: the snapshot's, copied into its run dir."""

    def __init__(self, snapshots: FrozenSnapshotStore, snapshot_id: str) -> None:
        self._snapshots = snapshots
        self._id = snapshot_id

    def stage(self, request: ImproverStagingRequest) -> StagedImproverInputs:
        data = self._snapshots.copy_inputs(self._id, request.run_dir)
        manifest = InputsManifest.model_validate_json((data / INPUTS_FILE).read_text(encoding="utf-8"))
        return StagedImproverInputs(data_dir=data, manifest=manifest, audit=audit_of(data))


class FrozenToolbox:
    """An empowered arm's toolbox: the snapshot's, copied into its run dir."""

    def __init__(self, snapshots: FrozenSnapshotStore, snapshot_id: str) -> None:
        self._snapshots = snapshots
        self._id = snapshot_id

    def stage(self, engine: EngineRef, run_dir: Path) -> ToolboxManifest:
        return self._snapshots.copy_toolbox(self._id, run_dir)


class _NoGitHub:
    """A tournament files nothing: any call to its effects' host is a defect."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"a tournament never touches GitHub (called {name})")


@dataclass(frozen=True)
class ArmOutput:
    arm: str
    heat: int
    #: The heat's accepted answer, or None if it produced none io accepted.
    text: str | None
    #: Paths that would name the arm to a grader (where the answer was written).
    hide: tuple[str, ...] = ()

    @property
    def output_id(self) -> str:
        return f"{self.arm}{self.heat}"


@dataclass(frozen=True)
class ArmSpec:
    """One arm as it runs: who answers, and with what prompt, heats and budget.

    A challenger differs from its champion in any of these (#8001 6b), and
    both run in one tournament on one snapshot.
    """

    arm: TournamentArm
    prompt: str
    #: The empowered mode's addendum (its toolbox and budget); unused by a scripted arm.
    empowered_addendum: str
    heats: HeatPlan
    #: The investigation budget the empowered agent is given.
    budget_minutes: int
    #: When one heat's agent is stopped.
    agent_timeout_minutes: int
    #: None: each heat is one graded output (an arm's answers, heat by heat).
    #: N: the arm runs N whole improver runs, each its heats merged, and
    #: each run's merged findings is one graded output: how a challenger to
    #: the improver is tried, since heats change what a run files (#8001).
    whole_runs: int | None = None

    @property
    def agent_runs(self) -> int:
        """How many agent runs the arm costs."""
        return self.heats.count * (self.whole_runs or 1)

    def __post_init__(self) -> None:
        if self.whole_runs is not None and self.whole_runs < 1:
            raise ValueError(f"arm {self.arm.name}: at least one whole run, not {self.whole_runs}")
        if self.arm.mode == "empowered" and not 0 < self.budget_minutes < self.agent_timeout_minutes:
            raise ValueError(
                f"arm {self.arm.name}: its {self.budget_minutes}-minute budget must be below its"
                f" {self.agent_timeout_minutes}-minute agent timeout, or the agent is stopped mid-budget"
            )


@dataclass(frozen=True)
class Grader:
    name: str
    choice: ImproverAgentChoice
    timeout_minutes: int = 40

    def __post_init__(self) -> None:
        require_slug(self.name, "a grader name")


def require_cross_model(graders: Sequence[Grader]) -> None:
    """Graders are cross-model: distinct names, at least two providers."""
    names = [g.name for g in graders]
    if len(set(names)) != len(names):
        raise ValueError(f"grader names repeat: {names}")
    if len({g.choice.provider for g in graders}) < 2:
        raise ValueError(
            f"a tournament is graded by at least two providers, not {sorted({g.choice.provider.value for g in graders})}"
        )


DEFAULT_GRADERS = (
    Grader("claude", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE)),
    Grader("codex", ImproverAgentChoice.for_provider(ImproverProvider.CODEX)),
)


class TournamentHarness:
    def __init__(
        self,
        *,
        root: Path,
        snapshots: FrozenSnapshotStore,
        keys: FileAnswerKeyStore,
        #: An agent for a choice, stopped after the given minutes.
        agent_for: Callable[[ImproverAgentChoice, int], ImproverAgent],
        grader_prompt: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._root = root / TOURNAMENTS_DIRNAME
        self._snapshots = snapshots
        self._keys = keys
        self._agent_for = agent_for
        self._grader_prompt = grader_prompt
        self._clock = clock

    def directory(self, tournament_id: str) -> Path:
        return self._root / require_slug(tournament_id, "a tournament id")

    def result_of(self, tournament_id: str) -> TournamentResult | None:
        """The tournament's result, if it was graded."""
        path = self.directory(tournament_id) / "result.json"
        return TournamentResult.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None

    def arms_ran(self, tournament_id: str) -> bool:
        """Whether the tournament's arms ran to the end (their outputs recorded)."""
        return (self.directory(tournament_id) / _ARMS_RUN).is_file()

    def run_arms(self, tournament_id: str, snapshot_id: str, specs: Sequence[ArmSpec]) -> list[ArmOutput]:
        """Each arm's heats on the snapshot, as outputs (a failed heat has none).

        The snapshot's answer key must exist first: a key is written before
        any result is seen, never after reading the arms' answers.
        """
        self._keys.get(snapshot_id)
        names = [spec.arm.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError(f"arm names repeat: {names}")
        outputs: list[ArmOutput] = []
        engine = self._snapshots.engine(snapshot_id)
        # A tournament's arms run once: a second run in the same tournament
        # (or a tournament id two runs share) is refused before any agent starts.
        try:
            (self.directory(tournament_id) / "arms").mkdir(parents=True)
        except FileExistsError:
            raise RuntimeError(f"tournament {tournament_id} has already run its arms") from None
        heats_by_provider: dict[str, int] = {}
        for spec in specs:
            provider = spec.arm.provider
            heats_by_provider[provider] = heats_by_provider.get(provider, 0) + spec.agent_runs
            outputs += self._run_arm(tournament_id, snapshot_id, engine, spec)
        # What these outputs were run on: grading them under another snapshot is refused.
        _write_atomic(self.directory(tournament_id) / _ARMS_RUN, json.dumps(
            {"snapshot_id": snapshot_id, "outputs": _digests(outputs), "heats_by_provider": heats_by_provider},
            indent=2,
        ) + "\n")
        return outputs

    def _run_arm(self, tournament_id: str, snapshot_id: str, engine: EngineRef, spec: ArmSpec) -> list[ArmOutput]:
        arm = spec.arm
        request = ImproverRunRequest(
            engine=engine, outputs_repo=NO_OUTPUTS_REPO, exam_dir=None, window=timedelta(hours=24), log_tail_bytes=1,
        )
        arm_dir = self.directory(tournament_id) / "arms" / arm.name
        if spec.whole_runs is not None:
            outputs = []
            for n in range(1, spec.whole_runs + 1):
                # Its own store: a run staged beside an earlier one would diff
                # against that run's audit, and the samples would differ.
                record = self._improver_run(FileImproverRunStore(arm_dir / f"run{n}"), snapshot_id, spec).run(
                    request, apply=False
                )
                run_dir = Path(record.run_dir)
                # A run io did not accept files nothing: it scores 0.
                accepted = record.outcome is RunOutcome.ACCEPTED
                text = (run_dir / FINDINGS_FILE).read_text(encoding="utf-8") if accepted else None
                outputs.append(ArmOutput(arm.name, n, text, hide=(str(run_dir.resolve()),)))
            return outputs
        record = self._improver_run(FileImproverRunStore(arm_dir), snapshot_id, spec).run(request, apply=False)
        run_dir = Path(record.run_dir)
        outputs = []
        for heat in range(1, spec.heats.count + 1):
            heat_record = next((h for h in record.heats if h.heat == heat), None)
            # Only an answer io accepted is graded: one it rejected would
            # file nothing, so it is worth nothing (it scores 0).
            accepted = heat_record is not None and heat_record.outcome is RunOutcome.ACCEPTED
            text = (run_dir / heat_file(FINDINGS_FILE, heat)).read_text(encoding="utf-8") if accepted else None
            outputs.append(ArmOutput(arm.name, heat, text, hide=(str(run_dir.resolve()),)))
        return outputs

    def _improver_run(self, store: FileImproverRunStore, snapshot_id: str, spec: ArmSpec) -> ImproverRun:
        arm = spec.arm
        return ImproverRun(
            store=store,
            stager=FrozenInputs(self._snapshots, snapshot_id),
            agent=self._agent_for(
                ImproverAgentChoice(provider=ImproverProvider(arm.provider), model=arm.model),
                spec.agent_timeout_minutes,
            ),
            investigation=self._investigation(spec, snapshot_id),
            effects=ImproverEffects(store=store, host=_NoGitHub(), outputs_repo=NO_OUTPUTS_REPO, clock=self._clock),  # type: ignore[arg-type]
            prompt=spec.prompt,
            heats=spec.heats,
            clock=self._clock,
        )

    def _investigation(self, spec: ArmSpec, snapshot_id: str) -> Any:
        if spec.arm.mode == "scripted":
            return ScriptedInvestigation()
        return EmpoweredInvestigation(
            stager=FrozenToolbox(self._snapshots, snapshot_id),
            # Live GitHub would show what was found after the snapshot.
            github=lambda repo: None,
            addendum=spec.empowered_addendum,
            budget_minutes=spec.budget_minutes,
        )

    def grade(
        self,
        tournament_id: str,
        snapshot_id: str,
        outputs: Sequence[ArmOutput],
        *,
        graders: Sequence[Grader] = DEFAULT_GRADERS,
        passes: int = DEFAULT_PASSES,
        seed: int,
    ) -> TournamentResult:
        """Anonymize the outputs, grade them ``passes`` times with every
        grader, pool the gradings and rank the arms within their noise.

        Cross-model means every grader: a tournament any grading failed to
        grade completely has no result (one model's judgment could rank it).
        Grading it again with the same outputs, seed and key reruns every
        grading on the same anonymized files (the earlier attempt is kept).
        """
        require_cross_model(graders)
        if passes < 1:
            raise ValueError(f"each grader grades at least once, not {passes} time(s)")
        key = self._keys.get(snapshot_id)
        root = self.directory(tournament_id).resolve()
        if (root / "result.json").exists():
            raise RuntimeError(f"tournament {tournament_id} already has a result")
        _require_run_on(root, snapshot_id, outputs)
        answered = [o for o in outputs if o.text is not None]
        labels = anonymize([o.output_id for o in answered], seed=seed)
        by_id = {o.output_id: o for o in answered}
        _prepare(root, _grading_inputs(root, labels, by_id, seed, key, snapshot_id, outputs))

        def grader_passes(grader: Grader) -> list[tuple[GraderRun, dict[str, float] | None]]:
            # A grader's passes one after another; graders side by side.
            return [self._grade_with(grader, n, root, labels, key) for n in range(1, passes + 1)]

        with ThreadPoolExecutor(max_workers=len(graders)) as executor:
            graded = dict(zip(graders, executor.map(grader_passes, graders), strict=True))
        runs = tuple(run for grader in graders for run, _ in graded[grader])
        refused = [run for run in runs if not run.accepted]
        if refused:
            raise RuntimeError(
                "not every grading was complete; no result (grade it again to retry): "
                + "; ".join(f"{run.grading}: {run.detail}" for run in refused)
            )
        gradings = {grader.name: [scores for _, scores in graded[grader] if scores is not None] for grader in graders}
        arm_of = {label: by_id[oid].arm for label, oid in labels.items()}
        ungraded = {o.output_id: o.arm for o in outputs if o.text is None}
        result = _result(
            tournament_id, snapshot_id, key, runs, passes, pool(gradings, arm_of, ungraded=ungraded, resolution=grade_step(key)),
            gradings, arm_of, ungraded, _cost(root),
        )
        _write_atomic(root / "result.json", result.model_dump_json(indent=2) + "\n")
        return result

    def regrade(
        self, tournament_id: str, *, graders: Sequence[Grader] = DEFAULT_GRADERS, passes: int = DEFAULT_PASSES
    ) -> TournamentResult:
        """Grade a tournament whose grading failed again: the same outputs,
        seed and snapshot, as its sealed request recorded them."""
        request = self.directory(tournament_id) / _REQUEST
        if not request.is_file():
            raise RuntimeError(f"tournament {tournament_id} was never prepared for grading")
        doc = json.loads(request.read_text(encoding="utf-8"))
        outputs = [ArmOutput(o["arm"], o["heat"], o["text"], tuple(o["hide"])) for o in doc["outputs"]]
        return self.grade(
            tournament_id, doc["snapshot_id"], outputs, graders=graders, passes=passes, seed=doc["seed"]
        )

    def _grade_with(
        self, grader: Grader, pass_number: int, root: Path, labels: Mapping[str, str], key: AnswerKey
    ) -> tuple[GraderRun, dict[str, float] | None]:
        """One grading, recorded (who, which pass, outcome, seconds) beside its
        answer whatever happens once the call starts, so a retried
        tournament still counts every call it made."""
        workdir = root / "graders" / grader.name / f"p{pass_number}"
        workdir.mkdir(parents=True)
        started = time.monotonic()

        def record(accepted: bool, detail: str) -> GraderRun:
            graded = GraderRun(
                name=grader.name, provider=grader.choice.provider.value, model=grader.choice.model,
                pass_number=pass_number, accepted=accepted, detail=detail[:500],
                seconds=round(time.monotonic() - started, 1),
            )
            _write_atomic(workdir / _GRADING_RECORD, graded.model_dump_json(indent=2) + "\n")
            return graded

        try:
            accepted, detail, scores = self._grade_once(grader, root, workdir, labels, key)
        except BaseException as error:
            record(False, f"raised: {error!r}")
            raise
        return record(accepted, detail), scores

    def _grade_once(
        self, grader: Grader, root: Path, workdir: Path, labels: Mapping[str, str], key: AnswerKey
    ) -> tuple[bool, str, dict[str, float] | None]:
        prompt = (
            self._grader_prompt.replace("<<COUNT>>", str(len(labels)))
            .replace("<<OUTPUTS_DIR>>", str(root / "anon"))
            .replace("<<KEY_FILE>>", str(root / "key" / "KEY.md"))
            .replace("<<LABELS>>", ", ".join(sorted(labels)))
            .replace("<<ITEM_IDS>>", ", ".join(i.id for i in key.scored))
        )
        space = HeatSpace(heat=1, run_dir=root, workdir=workdir, evidence=(root / "anon", root / "key"))
        answer = self._agent_for(grader.choice, grader.timeout_minutes).run(prompt=prompt, space=space, toolbox=None)
        if answer.final_message is None:
            return False, f"no answer: {answer.detail}", None
        (workdir / "grades.json").write_text(answer.final_message, encoding="utf-8")
        try:
            grades = read_grades(answer.final_message, {
                label: finding_ids((root / "anon" / f"{label}.json").read_text(encoding="utf-8")) for label in labels
            }, key)
        except GradesRejected as rejected:
            return False, f"rejected: {rejected}", None
        return True, f"graded {len(grades)} output(s)", {label: score(g, key) for label, g in grades.items()}


def render_key(key: AnswerKey) -> str:
    """The key as the graders read it: its preamble verbatim (what was
    audited, how to grade), then the scored items, each in its own words."""
    lines = [key.preamble or "Weights: 3 = major / systemic, 2 = real, 1 = minor.", "", "## Items", ""]
    for item in key.scored:
        joiner = "" if item.description[:1] in ",.;:)" else " "
        lines.append(f"- **{item.id}** ({item.weight}, {item.category}) **{item.title}**{joiner}{item.description}")
    return "\n".join(lines) + "\n"


#: Any path through io's improver store (every io improver run is written there).
_IMPROVER_STORE_PATH = re.compile(r"/[^\s\"']*/io-improver/[^\s\"']*")


def _scrubbed(text: str, hide: Sequence[str]) -> str:
    """The answer as a grader reads it: no path that names where (so by
    which arm) it was written. The audited engine's own paths stay: they
    are evidence, and the same for every arm.

    A JSON answer is scrubbed value by value after decoding (a path may be
    spelled with escaped slashes) and written out again.
    """
    body = findings_text(text)
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return _scrub_string(body, hide)
    return json.dumps(_scrub_json(doc, hide), indent=2, ensure_ascii=False) + "\n"


def _scrub_json(value: Any, hide: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _scrub_string(value, hide)
    if isinstance(value, list):
        return [_scrub_json(v, hide) for v in value]
    if isinstance(value, dict):
        return {_scrub_string(k, hide): _scrub_json(v, hide) for k, v in value.items()}
    return value


def _scrub_string(text: str, hide: Sequence[str]) -> str:
    for path in sorted(hide, key=len, reverse=True):
        text = re.sub(re.escape(path) + r"[^\s\"']*", "<RUN>", text)
    return _IMPROVER_STORE_PATH.sub("<RUN>", text)


def _grading_inputs(
    root: Path,
    labels: Mapping[str, str],
    by_id: Mapping[str, ArmOutput],
    seed: int,
    key: AnswerKey,
    snapshot_id: str,
    outputs: Sequence[ArmOutput],
) -> dict[str, str]:
    """What the graders read (``anon/``, ``key/``), the sealed mapping, and
    the sealed request a retry grades again, by relative path."""
    files = {
        f"anon/{label}.json": _scrubbed(by_id[oid].text or "", (str(root), *by_id[oid].hide))
        for label, oid in labels.items()
    }
    files["sealed/mapping.json"] = json.dumps(
        {"seed": seed, "labels": {label: {"output": oid, "arm": by_id[oid].arm} for label, oid in labels.items()}},
        indent=2,
    ) + "\n"
    files["key/KEY.md"] = render_key(key)
    files[_REQUEST] = json.dumps({
        "snapshot_id": snapshot_id, "seed": seed,
        "outputs": [{"arm": o.arm, "heat": o.heat, "text": o.text, "hide": list(o.hide)} for o in outputs],
    }, indent=2) + "\n"
    return files


def _prepare(root: Path, files: Mapping[str, str]) -> None:
    """Write the grading inputs, or, on a retry, require they are unchanged.

    The mapping is written last: without it, nothing was prepared (a
    half-written attempt is cleared). With it, every file must match, and
    the earlier graders' answers are kept beside the new attempt's.
    """
    mapping = root / "sealed" / "mapping.json"
    if mapping.exists():
        present = {str(p.relative_to(root)) for d in ("anon", "sealed", "key") for p in (root / d).iterdir()}
        changed = sorted(
            name for name in present | set(files)
            if name not in files or name not in present or (root / name).read_text(encoding="utf-8") != files[name]
        )
        if changed:
            raise RuntimeError(f"a retry must grade the same outputs with the same seed and key; changed: {changed}")
        graders = root / "graders"
        if graders.exists():
            graders.rename(root / f"graders-attempt-{len(list(root.glob('graders-attempt-*'))) + 1}")
        return
    for directory in ("anon", "sealed", "key"):
        shutil.rmtree(root / directory, ignore_errors=True)
        (root / directory).mkdir(parents=True)
    for name, text in sorted(files.items(), key=lambda item: item[0] == "sealed/mapping.json"):
        _write_atomic(root / name, text)


def _require_run_on(root: Path, snapshot_id: str, outputs: Sequence[ArmOutput]) -> None:
    """Outputs this tournament's arms produced are graded only as what they
    are: the same outputs, on the snapshot they were run on."""
    record = root / _ARMS_RUN
    if not record.is_file():
        return
    ran = json.loads(record.read_text(encoding="utf-8"))
    if ran["snapshot_id"] != snapshot_id:
        raise RuntimeError(f"the arms ran on snapshot {ran['snapshot_id']}, not {snapshot_id}; no grading")
    if ran["outputs"] != _digests(outputs):
        raise RuntimeError(f"the arms produced {sorted(ran['outputs'])}, not these outputs; no grading")


def _digests(outputs: Sequence[ArmOutput]) -> dict[str, str | None]:
    """Each output's identity: its id and the bytes it answered (None: no answer)."""
    return {
        o.output_id: None if o.text is None else hashlib.sha256(o.text.encode("utf-8")).hexdigest()
        for o in sorted(outputs, key=lambda o: o.output_id)
    }


def _write_atomic(path: Path, text: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(temporary, path)


def _result(
    tournament_id: str,
    snapshot_id: str,
    key: AnswerKey,
    runs: tuple[GraderRun, ...],
    passes: int,
    pooled: PooledScores,
    gradings: Mapping[str, Sequence[Mapping[str, float]]],
    arm_of: Mapping[str, str],
    ungraded: Mapping[str, str],
    cost: TournamentCost,
) -> TournamentResult:
    """Each arm's pooled score with its noise, the ranking, and the cost."""
    arms: list[ArmScore] = []
    for arm in sorted(pooled.arms):
        p = pooled.arms[arm]
        no_answer = [0.0] * sum(1 for a in ungraded.values() if a == arm)
        arms.append(ArmScore(
            arm=arm,
            scores={
                f"{grader}#{n}": tuple(sorted([*(v for label, v in by_label.items() if arm_of[label] == arm), *no_answer]))
                for grader, runs_ in gradings.items()
                for n, by_label in enumerate(runs_, 1)
            },
            output_means=tuple(round(v, 3) for v in p.output_means),
            # Ranked on the exact means; only what is shown is rounded.
            mean=round(p.mean, 3),
            se=round(p.se, 3),
            low=min(p.output_means),
            high=max(p.output_means),
        ))
    means = {arm: p.mean for arm, p in pooled.arms.items()}
    ordered = sorted(means, key=lambda arm: (-means[arm], arm))
    return TournamentResult(
        tournament_id=tournament_id, snapshot_id=snapshot_id, key_items=len(key.scored),
        max_score=key.max_score, graders=runs, passes=passes,
        noise=TournamentNoise(
            heat=_rounded(pooled.noise.heat), grader=round(pooled.noise.grader, 4),
            pass_=round(pooled.noise.pass_, 4), resolution=pooled.resolution,
        ),
        band_ses=NOISE_BAND_SES, heat_alpha=ALPHA, arms=tuple(arms),
        ranking=rank(means, distinguishable=pooled.distinguishable),
        comparisons=tuple(
            ArmComparison(
                # Exact: the record must decide as the comparison did (only display rounds).
                higher=a, lower=b, gap=means[a] - means[b], band=pooled.band(a, b),
                heat_p=pooled.heat_p(a, b), distinguishable=pooled.distinguishable(a, b),
            )
            for i, a in enumerate(ordered) for b in ordered[i + 1:]
        ),
        cost=cost,
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def grade_step(key: AnswerKey) -> float:
    """The smallest score step a grading expresses: half credit on the key's lightest scored item."""
    return GRADE_CREDIT["half"] * min(item.weight for item in key.scored)


def _cost(root: Path) -> TournamentCost:
    """What the tournament spent: its arms' heats, and every grading call
    made for it, retried attempts included."""
    record = root / _ARMS_RUN
    heats = dict(json.loads(record.read_text(encoding="utf-8"))["heats_by_provider"]) if record.is_file() else {}
    calls: dict[str, int] = {}
    seconds: dict[str, float] = {}
    for path in sorted(root.glob(f"graders*/*/p*/{_GRADING_RECORD}")):
        run = GraderRun.model_validate_json(path.read_text(encoding="utf-8"))
        calls[run.provider] = calls.get(run.provider, 0) + 1
        seconds[run.provider] = round(seconds.get(run.provider, 0.0) + run.seconds, 1)
    return TournamentCost(arm_heats=heats, grader_calls=calls, grader_seconds=seconds)


__all__ = [
    "DEFAULT_GRADERS",
    "DEFAULT_PASSES",
    "NO_OUTPUTS_REPO",
    "TOURNAMENTS_DIRNAME",
    "ArmOutput",
    "ArmSpec",
    "FrozenInputs",
    "FrozenToolbox",
    "Grader",
    "TournamentHarness",
    "render_key",
    "require_cross_model",
]
