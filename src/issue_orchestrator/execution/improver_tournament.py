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

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..contracts.improver_findings import FINDINGS_FILE
from ..contracts.improver_inputs import INPUTS_FILE, InputsManifest
from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider, RunOutcome
from ..contracts.improver_toolbox import ToolboxManifest
from ..contracts.improver_tournament import (
    AnswerKey,
    ArmScore,
    GraderRun,
    TournamentArm,
    TournamentResult,
    require_slug,
)
from ..domain.engine_activity import EngineRef
from ..domain.improver_tournament import GradesRejected, anonymize, arm_means, rank, read_grades, score
from ..entrypoints.improver_run import HeatPlan, ImproverRun, ImproverRunRequest, findings_text
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
TIE_MARGIN = 0.5


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
    #: The heat's answer, or None if it produced none.
    text: str | None

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

    def __post_init__(self) -> None:
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

    def run_arms(self, tournament_id: str, snapshot_id: str, specs: Sequence[ArmSpec]) -> list[ArmOutput]:
        """Each arm's heats on the snapshot, as outputs (a failed heat has none)."""
        names = [spec.arm.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError(f"arm names repeat: {names}")
        outputs: list[ArmOutput] = []
        engine = self._snapshots.engine(snapshot_id)
        for spec in specs:
            arm, heats = spec.arm, spec.heats
            store = FileImproverRunStore(self.directory(tournament_id) / "arms" / arm.name)
            run = ImproverRun(
                store=store,
                stager=FrozenInputs(self._snapshots, snapshot_id),
                agent=self._agent_for(
                    ImproverAgentChoice(provider=ImproverProvider(arm.provider), model=arm.model),
                    spec.agent_timeout_minutes,
                ),
                investigation=self._investigation(spec, snapshot_id),
                effects=ImproverEffects(store=store, host=_NoGitHub(), outputs_repo=NO_OUTPUTS_REPO, clock=self._clock),  # type: ignore[arg-type]
                prompt=spec.prompt,
                heats=heats,
                clock=self._clock,
            )
            record = run.run(
                ImproverRunRequest(
                    engine=engine, outputs_repo=NO_OUTPUTS_REPO, exam_dir=None,
                    window=timedelta(hours=24), log_tail_bytes=1,
                ),
                apply=False,
            )
            run_dir = Path(record.run_dir)
            for heat in range(1, heats.count + 1):
                answer = run_dir / heat_file(FINDINGS_FILE, heat)
                heat_record = next((h for h in record.heats if h.heat == heat), None)
                has_answer = heat_record is not None and heat_record.outcome is not RunOutcome.AGENT_FAILED
                outputs.append(ArmOutput(arm.name, heat, answer.read_text(encoding="utf-8") if has_answer else None))
        return outputs

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
        seed: int,
    ) -> TournamentResult:
        """Anonymize the outputs, grade them with every grader, rank the arms.

        Cross-model means every grader: a tournament one grader failed to
        grade completely has no result (one model's judgment could rank it).
        """
        require_cross_model(graders)
        key = self._keys.get(snapshot_id)
        root = self.directory(tournament_id).resolve()
        anon, sealed, key_dir = root / "anon", root / "sealed", root / "key"
        for d in (anon, sealed, key_dir):
            d.mkdir(parents=True, exist_ok=False)
        answered = [o for o in outputs if o.text is not None]
        labels = anonymize([o.output_id for o in answered], seed=seed)
        by_id = {o.output_id: o for o in answered}
        for label, output_id in labels.items():
            (anon / f"{label}.json").write_text(_scrubbed(by_id[output_id].text or "", root), encoding="utf-8")
        (sealed / "mapping.json").write_text(json.dumps(
            {"seed": seed, "labels": {label: {"output": oid, "arm": by_id[oid].arm} for label, oid in labels.items()}},
            indent=2,
        ) + "\n", encoding="utf-8")
        (key_dir / "KEY.md").write_text(render_key(key), encoding="utf-8")

        runs: list[GraderRun] = []
        per_grader: dict[str, dict[str, float]] = {}
        for grader in graders:
            accepted, detail, scores = self._grade_with(grader, root, labels, key)
            runs.append(GraderRun(
                name=grader.name, provider=grader.choice.provider.value, model=grader.choice.model,
                accepted=accepted, detail=detail,
            ))
            if scores is not None:
                per_grader[grader.name] = {
                    **scores,
                    # A heat with no answer scores 0 for every grader.
                    **{f"(none){o.output_id}": 0.0 for o in outputs if o.text is None},
                }
        refused = [run for run in runs if not run.accepted]
        if refused:
            raise RuntimeError(
                "not every grader produced a complete grading; no result: "
                + "; ".join(f"{run.name}: {run.detail}" for run in refused)
            )
        arm_of = {**{label: by_id[oid].arm for label, oid in labels.items()},
                  **{f"(none){o.output_id}": o.arm for o in outputs if o.text is None}}
        result = _result(tournament_id, snapshot_id, key, runs, per_grader, arm_of, outputs)
        (root / "result.json").write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return result

    def _grade_with(
        self, grader: Grader, root: Path, labels: Mapping[str, str], key: AnswerKey
    ) -> tuple[bool, str, dict[str, float] | None]:
        workdir = root / "graders" / grader.name
        workdir.mkdir(parents=True)
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
            grades = read_grades(answer.final_message, labels, key)
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


def _scrubbed(text: str, root: Path) -> str:
    """The answer as a grader reads it: no path that names its arm."""
    return findings_text(re.sub(re.escape(str(root)) + r"[^\s\"']*", "<RUN>", text))


def _result(
    tournament_id: str,
    snapshot_id: str,
    key: AnswerKey,
    runs: list[GraderRun],
    per_grader: dict[str, dict[str, float]],
    arm_of: Mapping[str, str],
    outputs: Sequence[ArmOutput],
) -> TournamentResult:
    """Each arm's scores (per grader, over its outputs) and the ranking."""
    pooled = arm_means(per_grader, arm_of)
    # Ranked on the exact means; only what is shown is rounded.
    means = {arm: sum(values) / len(values) for arm, values in pooled.items()}
    scores: list[ArmScore] = []
    for arm in sorted({o.arm for o in outputs}):
        values = pooled[arm]
        scores.append(ArmScore(
            arm=arm,
            scores={
                grader: tuple(sorted(v for label, v in by_label.items() if arm_of[label] == arm))
                for grader, by_label in per_grader.items()
            },
            mean=round(means[arm], 3),
            low=min(values),
            high=max(values),
        ))
    return TournamentResult(
        tournament_id=tournament_id, snapshot_id=snapshot_id, key_items=len(key.scored),
        max_score=key.max_score, graders=tuple(runs), arms=tuple(scores),
        ranking=rank(means, tie_margin=TIE_MARGIN), tie_margin=TIE_MARGIN,
    )


__all__ = [
    "DEFAULT_GRADERS",
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
