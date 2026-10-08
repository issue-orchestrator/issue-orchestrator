#!/usr/bin/env python3
"""The tech-lead improver's orchestrator side (#7490).

    improver run --outputs-repo issue-orchestrator/issue-orchestrator \
        [--provider claude|codex] [--model M] [--mode empowered|scripted] [--budget-minutes 60] \
        [--apply] [--exam-dir D] [--engine-source-repo .] [--recent-hours 24]
    improver run --state-dir ~/dev/porchpin/.issue-orchestrator/state \
        --audited-repo porchpin/porchpin --outputs-repo issue-orchestrator/issue-orchestrator \
        [--exclude-open-issue N ...]
    improver status
    improver apply --outputs-repo issue-orchestrator/issue-orchestrator
    improver stage ... --run-dir RUN [--previous-audit A]
    improver validate --run-dir RUN

(each ``python -m issue_orchestrator.entrypoints.cli_tools.improver ...``)

``run`` is the whole daily run (:mod:`..improver_run`): stage the inputs, run
the improver read-only, validate its findings strictly and record the run.
It is a DRY RUN unless ``--apply``: an accepted run's GitHub effects are
recorded as owed (``improver apply`` applies them later). ``--mode
empowered`` (the default, #8001) gives the agent the read-only toolbox and
``--budget-minutes`` to choose its depth in; ``scripted`` gives it the
staged bundle alone. The agent runs on
``--provider`` (default: the latest improver tournament's winner, Claude) and
``--model`` (default: that provider's default model). Without ``--state-dir``
it sweeps every engine Control Center runs or ran within ``--recent-hours``
(:mod:`..improver_sweep`), one improver run per engine; with it, it audits
that one engine. ``status`` prints the
recorded runs; ``apply`` retries effects a rate limit left pending.
``stage`` and ``validate`` run one step alone, for an operator.

Exit codes follow the budgeted-validation convention: 0 success, 1 a rejected
findings file, 75 an input or agent that was unavailable. The run store is
``<git common dir>/io-improver`` of the checkout the command runs in.

This is the composition root: the one place that picks the GitHub host, the
runners, the store and the clock and hands them to their owners.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...contracts.engine_audit import SourceStatus
from ...contracts.improver_findings import FINDINGS_FILE
from ...contracts.improver_inputs import IMPROVER_DATA_DIRNAME
from ...contracts.improver_run import (
    DEFAULT_IMPROVER_AGENT,
    ImproverAgentChoice,
    ImproverProvider,
)
from ...contracts.improver_toolbox import DEFAULT_IMPROVER_MODE, ImproverMode
from ...contracts.improver_variant import ImproverVariant
from ...domain.engine_activity import EngineInventoryRead, EngineRef, EngineSighting
from ...domain.improver_answer import AnswerNotExtractable, extract_findings_answer
from ...domain.improver_champion import (
    INVITATION_RATE,
    RUN_BUDGET_MINUTES,
    ChangeInvitation,
    run_limits,
)
from ...domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    validate_findings,
)
from ...execution.command_runner import LocalCommandRunner
from ...execution.engine_inventory import control_center_engine_inventory, engine_at
from ...execution.engine_source_archive import GitEngineSourceArchive
from ...execution.improver_agents import improver_agent
from ...execution.improver_champion_store import FileChampionStore
from ...execution.improver_effect_applier import ImproverEffects
from ...execution.improver_investigation import (
    EMPOWERED_ADDENDUM,
    EmpoweredInvestigation,
    ScriptedInvestigation,
)
from ...execution.improver_run_store import FileImproverRunStore, improver_root
from ...execution.improver_toolbox_staging import ImproverToolboxStager
from ...execution.process_group_command_runner import ProcessGroupCommandRunner
from ...execution.providers import (
    create_audited_repo_reads,
    create_operator_activity_source,
    create_repository_host,
)
from ...observation.engine_audit import Unavailable
from ...ports.engine_activity import EngineInventory
from ...ports.improver import ImproverStoreBusy, heat_file
from ...ports.improver_investigation import ImproverInvestigation
from ..improver_run import (
    CHANGE_INVITATION_FILE,
    ChangePolicy,
    HeatPlan,
    ImproverRun,
    render_run,
)
from ..improver_staging import (
    ImproverInputStager,
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    load_staged_evidence,
)
from ..improver_sweep import ImproverSweep, ImproverSweepRequest

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNAVAILABLE = 75


#: Heats per engine: two, so a finding found twice stands out, at twice one
#: run's cost (#8001); both at once, so a run takes one heat's time.
DEFAULT_HEATS = 2
#: The budgeted suite allows 120 minutes; staging and the toolbox take the rest.

#: The prompt, relative to the io checkout the command runs in.
DEFAULT_PROMPT = Path("examples/prompts/tech-lead-improver.md")
#: The invitation a champion run sometimes gets: propose one change (#8001).
CHANGE_ADDENDUM = Path("examples/prompts/improver-change-addendum.md")
DEFAULT_BUDGET_MINUTES = 60


def _engine_arguments(parser: argparse.ArgumentParser, *, one_engine: bool) -> None:
    parser.add_argument(
        "--state-dir", required=one_engine, type=Path,
        help="The audited engine's state dir" + ("" if one_engine else " (default: every engine Control Center runs)"),
    )
    parser.add_argument("--audited-repo", required=one_engine, help="owner/repo the audited engine works")
    parser.add_argument("--outputs-repo", required=True, help="owner/repo the improver's outputs are filed in")
    parser.add_argument("--exam-dir", type=Path, help="Where the tech-lead exam writes scorecards")
    parser.add_argument(
        "--engine-source-repo", type=Path, default=Path("."),
        help="An io repository holding the engine's commit (default: .)",
    )
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--log-tail-mb", type=int, default=64)
    parser.add_argument("--no-github", action="store_true", help="Do not read the audited repo's GitHub")
    parser.add_argument(
        "--exclude-open-issue", type=int, action="append", default=[], metavar="N",
        help="Blind test: hide outputs-repo issue N from open-issues.json (repeatable). A run with"
        " it cannot --apply and files nothing",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="improver", description="The tech-lead improver's orchestrator side (#7490)")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="Stage the improver's inputs")
    _engine_arguments(stage, one_engine=True)
    stage.add_argument("--run-dir", required=True, type=Path)
    stage.add_argument("--previous-audit", type=Path, help="The previous run's audit.json")
    validate = sub.add_parser("validate", help="Validate improver-findings.json (or one heat's answer)")
    validate.add_argument("--run-dir", required=True, type=Path)
    validate.add_argument(
        "--heat", type=int,
        help="Validate heat N's answer (improver-findings-hN.json), its findings found in it as a run finds them",
    )
    run = sub.add_parser("run", help="Stage, run the improver agent, validate, record and apply")
    _engine_arguments(run, one_engine=False)
    run.add_argument(
        "--recent-hours", type=float, default=24.0,
        help="Sweep: an engine never audited before that stopped longer ago than this is not audited",
    )
    # Unset, these are the improver champion's (#8001), or with no champion
    # yet the defaults below; a run that sets none of them runs the champion.
    run.add_argument(
        "--provider", type=ImproverProvider, choices=list(ImproverProvider),
        help=f"The agent CLI the improver runs on (default: the champion's, else {DEFAULT_IMPROVER_AGENT.provider})",
    )
    run.add_argument("--model", help="The model the improver runs on (default: the champion's, else the provider's)")
    run.add_argument(
        "--agent-timeout-minutes", type=int,
        help="default: 90, or for an empowered budget, the budget plus 15 (never mid-budget)",
    )
    run.add_argument("--prompt", type=Path, help=f"default: the champion's, else {DEFAULT_PROMPT}")
    run.add_argument(
        "--mode", type=ImproverMode, choices=list(ImproverMode),
        help="empowered: the staged bundle plus the read-only toolbox; scripted: the bundle alone"
        f" (default: the champion's, else {DEFAULT_IMPROVER_MODE})",
    )
    run.add_argument(
        "--budget-minutes", type=int,
        help="Empowered: the investigation budget the agent is given (below the agent timeout;"
        f" default: the champion's, else {DEFAULT_BUDGET_MINUTES})",
    )
    run.add_argument(
        "--change-invitation-rate", type=float, default=INVITATION_RATE,
        help="The share of champion runs invited to propose one change to the improver (default: %(default)s;"
        " 0: never)",
    )
    run.add_argument("--change-addendum", type=Path, default=CHANGE_ADDENDUM)
    run.add_argument("--empowered-addendum", type=Path, default=EMPOWERED_ADDENDUM)
    run.add_argument(
        "--heats", type=int,
        help=f"Independent agent runs per engine, merged (default: the champion's, else {DEFAULT_HEATS})."
        " Each is a whole agent run on the provider: a Claude heat counts against the subscription",
    )
    run.add_argument(
        "--parallel-heats", type=int,
        help="Heats run at once (default: all of them, one wave)",
    )
    run.add_argument(
        "--run-budget-minutes", type=int, default=RUN_BUDGET_MINUTES,
        help="The longest the heats may take, one wave after another, each wave up to"
        " --agent-timeout-minutes (default: %(default)s, inside the budgeted suite's timeout)",
    )
    run.add_argument(
        "--apply", action="store_true",
        help="Apply accepted findings' GitHub effects; without it the run is dry and they stay owed",
    )
    apply = sub.add_parser("apply", help="Apply what accepted runs still owe GitHub")
    apply.add_argument("--outputs-repo", required=True)
    sub.add_parser("status", help="Print the recorded runs, newest first")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "stage":
        return stage(args)
    if args.command == "run":
        return run(args)
    if args.command == "apply":
        return apply(args.outputs_repo)
    if args.command == "status":
        return status()
    return validate(args.run_dir, heat=args.heat)


def _stager(args: argparse.Namespace, audited_repo: str) -> ImproverInputStager:
    if args.window_hours <= 0 or args.log_tail_mb <= 0:
        raise SystemExit("improver: --window-hours and --log-tail-mb must be positive")
    outputs = create_repository_host(args.outputs_repo)
    audited = (
        Unavailable(SourceStatus.SKIPPED, "--no-github")
        if args.no_github
        else outputs
        if audited_repo == args.outputs_repo
        else create_repository_host(audited_repo)
    )
    return ImproverInputStager(
        audited_host=audited,
        outputs_host=outputs,
        source=GitEngineSourceArchive(args.engine_source_repo.resolve(), LocalCommandRunner()),
        activity=Unavailable(SourceStatus.SKIPPED, "--no-github")
        if args.no_github
        else create_operator_activity_source(audited_repo),
        clock=_now,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def stage(args: argparse.Namespace) -> int:
    try:
        staged = _stager(args, args.audited_repo).stage(
            ImproverStagingRequest(
                engine=engine_at(args.state_dir.expanduser().resolve(), args.audited_repo),
                outputs_repo=args.outputs_repo,
                run_dir=args.run_dir.expanduser().resolve(),
                previous_audit=args.previous_audit,
                exam_dir=args.exam_dir,
                window=timedelta(hours=args.window_hours),
                log_tail_bytes=args.log_tail_mb * 1024 * 1024,
                excluded_open_issues=frozenset(args.exclude_open_issue),
            )
        )
    except ImproverInputsUnavailable as error:
        print(f"improver stage: unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    for entry in staged.manifest.inputs:
        print(f"{'staged ' if entry.staged else 'MISSING'} {entry.name}: {entry.detail}")
    return EXIT_OK


def _store() -> FileImproverRunStore:
    return FileImproverRunStore.for_checkout(Path.cwd(), LocalCommandRunner())


def _effects(outputs_repo: str, store: FileImproverRunStore) -> ImproverEffects:
    return ImproverEffects(
        store=store, host=create_repository_host(outputs_repo), outputs_repo=outputs_repo, clock=_now
    )


def _investigation(args: argparse.Namespace) -> ImproverInvestigation:
    if args.mode is ImproverMode.SCRIPTED:
        return ScriptedInvestigation()
    return EmpoweredInvestigation(
        stager=ImproverToolboxStager(runner=LocalCommandRunner(), clock=_now),
        github=lambda repo: None if args.no_github else create_audited_repo_reads(repo),
        addendum=args.empowered_addendum.read_text(encoding="utf-8"),
        budget_minutes=args.budget_minutes,
    )


def agent_choice(args: argparse.Namespace) -> ImproverAgentChoice:
    """The provider and model ``run`` launches the improver on (settled args)."""
    return ImproverAgentChoice.for_provider(args.provider, args.model)


@dataclass(frozen=True)
class Champion:
    variant: ImproverVariant
    prompt: str


def settle(args: argparse.Namespace, champion: Champion | None) -> argparse.Namespace:
    """``run``'s arguments with every unset improver setting filled: the
    champion's when there is one, else the defaults. ``runs_champion``: the
    run is exactly the champion (only such a run may be invited to change it)."""
    chosen = {name: getattr(args, name) for name in ("provider", "model", "prompt", "mode", "budget_minutes", "heats")}
    if champion is not None:
        v = champion.variant
        defaults = {"provider": v.agent.provider, "model": v.agent.model, "mode": v.mode,
                    "budget_minutes": v.budget_minutes, "heats": v.heats}
        prompt = chosen["prompt"].read_text(encoding="utf-8") if chosen["prompt"] is not None else champion.prompt
    else:
        defaults = {"provider": DEFAULT_IMPROVER_AGENT.provider, "model": None, "mode": DEFAULT_IMPROVER_MODE,
                    "budget_minutes": DEFAULT_BUDGET_MINUTES, "heats": DEFAULT_HEATS}
        prompt = (chosen["prompt"] or DEFAULT_PROMPT).read_text(encoding="utf-8")
    settled = {name: defaults[name] if value is None else value for name, value in chosen.items() if name != "prompt"}
    if chosen["provider"] is not None and chosen["model"] is None:
        settled["model"] = None  # another provider: its own default model
    runs_champion = champion is not None and all(value is None for value in chosen.values())
    # Unset limits follow the settled settings, as a live champion run's do
    # (domain.improver_champion.live_limits): all heats in one wave, an agent
    # timeout never shorter than its budget.
    limits = run_limits(
        settled["mode"], settled["budget_minutes"], settled["heats"],
        agent_timeout=args.agent_timeout_minutes, parallel=args.parallel_heats,
    )
    return argparse.Namespace(**{
        **vars(args), **settled, "parallel_heats": limits.parallel_heats,
        "agent_timeout_minutes": limits.agent_timeout_minutes, "prompt_text": prompt, "runs_champion": runs_champion,
    })


def refuse_contradictions(args: argparse.Namespace) -> None:
    """``run``'s options that cannot hold together end the command at once."""
    if (args.state_dir is None) != (args.audited_repo is None):
        raise SystemExit("improver run: --state-dir and --audited-repo go together")
    if args.exclude_open_issue and args.apply:
        raise SystemExit("improver run: --exclude-open-issue is a blind run; it cannot --apply")
    try:
        HeatPlan(count=args.heats, parallel=args.parallel_heats).require_within(
            agent_timeout_minutes=args.agent_timeout_minutes, budget_minutes=args.run_budget_minutes
        )
    except ValueError as error:
        raise SystemExit(f"improver run: --heats/--parallel-heats: {error}") from error
    if args.mode is ImproverMode.EMPOWERED and args.budget_minutes >= args.agent_timeout_minutes:
        raise SystemExit("improver run: --budget-minutes must be below --agent-timeout-minutes")


def run(args: argparse.Namespace) -> int:
    root = improver_root(Path.cwd(), LocalCommandRunner())
    champions = FileChampionStore(root)
    champion = None
    if champions.seeded():
        variant = champions.state().champion
        champion = Champion(variant, champions.prompt(variant.prompt_sha256))
    args = settle(args, champion)
    refuse_contradictions(args)
    store = _store()
    prompt = args.prompt_text
    investigation = _investigation(args)
    policy = (
        ChangePolicy(champion.variant, champion.prompt, args.change_addendum.read_text(encoding="utf-8"),
                     rate=args.change_invitation_rate)
        if champion is not None and args.runs_champion and args.change_invitation_rate > 0
        else None
    )

    def improver_for(engine: EngineRef) -> ImproverRun:
        return ImproverRun(
            store=store,
            stager=_stager(args, engine.repo),
            agent=improver_agent(
                agent_choice(args),
                runner=ProcessGroupCommandRunner(),
                timeout_seconds=args.agent_timeout_minutes * 60,
            ),
            investigation=investigation,
            effects=_effects(args.outputs_repo, store),
            prompt=prompt,
            heats=HeatPlan(count=args.heats, parallel=args.parallel_heats),
            clock=_now,
            change_policy=policy,
        )

    request = ImproverSweepRequest(
        outputs_repo=args.outputs_repo,
        exam_dir=args.exam_dir,
        window=timedelta(hours=args.window_hours),
        log_tail_bytes=args.log_tail_mb * 1024 * 1024,
        recent=timedelta(hours=args.recent_hours),
        excluded_open_issues=frozenset(args.exclude_open_issue),
    )
    inventory: EngineInventory = (
        control_center_engine_inventory()
        if args.state_dir is None
        else _OneEngine(engine_at(args.state_dir.expanduser().resolve(), args.audited_repo))
    )
    try:
        result = ImproverSweep(
            inventory=inventory, runs=store, effects=_effects(args.outputs_repo, store),
            run_for=improver_for, clock=_now,
        ).sweep(
            request, apply=args.apply
        )
    except ImproverStoreBusy as busy:
        print(f"improver run: {busy}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    for missing in result.unidentified:
        print(f"improver run: engine at {missing.state_dir} not audited: {missing.reason}", file=sys.stderr)
    if not result.engines:
        print("improver run: no engine ran since it was last audited; nothing to audit", file=sys.stderr)
    for record in result.runs:
        print(render_run(record))
    if result.owed_by and args.apply:
        print(f"improver run: effects still owed by {', '.join(result.owed_by)}", file=sys.stderr)
    # A dry run leaves effects owed on purpose; only outcomes count.
    return result.exit_code


class _OneEngine:
    """The inventory of an explicitly named engine."""

    def __init__(self, engine: EngineRef) -> None:
        self._engine = engine

    def engines(self, *, since: datetime) -> EngineInventoryRead:
        # Named explicitly: audited whatever it did lately.
        return EngineInventoryRead(sightings=(EngineSighting(self._engine, running=True, last_written=None),))


def apply(outputs_repo: str) -> int:
    store = _store()
    try:
        with store.exclusive():
            runs = _effects(outputs_repo, store).apply_pending()
    except ImproverStoreBusy as busy:
        print(f"improver apply: {busy}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    for record in runs:
        print(render_run(record))
    return max((r.exit_code for r in runs), default=EXIT_OK)


def status() -> int:
    runs = _store().runs()
    if not runs:
        print("No improver runs recorded.")
    for record in runs:
        print(render_run(record))
    return EXIT_OK


def validate(run_dir: Path, *, heat: int | None = None) -> int:
    """The run's findings file, or heat ``heat``'s stored answer, validated
    offline (no agent runs): an answer is read as a run reads it, its one
    findings document found among any prose (which is reported)."""
    evidence = load_staged_evidence(run_dir / IMPROVER_DATA_DIRNAME)
    name = FINDINGS_FILE if heat is None else heat_file(FINDINGS_FILE, heat)
    path = run_dir / name
    if not path.is_file():
        print(f"[{Rule.SCHEMA.value}] the improver wrote no {name}")
        return EXIT_REJECTED
    try:
        extracted = extract_findings_answer(path.read_text(encoding="utf-8"))
    except AnswerNotExtractable as error:
        print(f"[{Rule.SCHEMA.value}] <file>: {error}")
        return EXIT_REJECTED
    if extracted.discarded:
        print(f"discarded {len(extracted.discarded)} character(s) of prose around the findings document")
    invited = run_dir / CHANGE_INVITATION_FILE
    invitation = ChangeInvitation.from_json(invited.read_text(encoding="utf-8")) if invited.is_file() else None
    try:
        findings = validate_findings(extracted.text, evidence, invitation=invitation)
    except ImproverFindingsRejected as rejection:
        for violation in rejection.violations:
            print(violation.describe())
        return EXIT_REJECTED
    print(f"valid: {len(findings.findings)} finding(s), {len(findings.design_findings)} design finding(s)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
