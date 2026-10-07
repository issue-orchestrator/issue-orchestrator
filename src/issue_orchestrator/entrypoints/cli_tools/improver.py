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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...domain.engine_activity import EngineInventoryRead, EngineRef, EngineSighting
from ...execution.engine_inventory import control_center_engine_inventory, engine_at
from ...execution.engine_source_archive import GitEngineSourceArchive
from ...ports.engine_activity import EngineInventory
from ...contracts.engine_audit import SourceStatus
from ...contracts.improver_findings import FINDINGS_FILE
from ...contracts.improver_inputs import IMPROVER_DATA_DIRNAME
from ...domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    validate_findings,
)
from ...execution.command_runner import LocalCommandRunner
from ...execution.providers import create_audited_repo_reads, create_repository_host
from ...observation.engine_audit import Unavailable
from ...execution.improver_effect_applier import ImproverEffects
from ...contracts.improver_run import DEFAULT_IMPROVER_AGENT, ImproverAgentChoice, ImproverProvider
from ...contracts.improver_toolbox import DEFAULT_IMPROVER_MODE, ImproverMode
from ...execution.improver_agents import improver_agent
from ...execution.improver_investigation import EMPOWERED_ADDENDUM, EmpoweredInvestigation, ScriptedInvestigation
from ...execution.improver_toolbox_staging import ImproverToolboxStager
from ...ports.improver_investigation import ImproverInvestigation
from ...execution.improver_run_store import FileImproverRunStore
from ...ports.improver import ImproverStoreBusy
from ...execution.process_group_command_runner import ProcessGroupCommandRunner
from ..improver_run import ImproverRun, render_run
from ..improver_sweep import ImproverSweep, ImproverSweepRequest
from ..improver_staging import (
    ImproverInputStager,
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    load_staged_evidence,
)


EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNAVAILABLE = 75


#: The prompt, relative to the io checkout the command runs in.
DEFAULT_PROMPT = Path("examples/prompts/tech-lead-improver.md")


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
    validate = sub.add_parser("validate", help="Validate improver-findings.json")
    validate.add_argument("--run-dir", required=True, type=Path)
    run = sub.add_parser("run", help="Stage, run the improver agent, validate, record and apply")
    _engine_arguments(run, one_engine=False)
    run.add_argument(
        "--recent-hours", type=float, default=24.0,
        help="Sweep: an engine never audited before that stopped longer ago than this is not audited",
    )
    run.add_argument(
        "--provider", type=ImproverProvider, choices=list(ImproverProvider),
        default=DEFAULT_IMPROVER_AGENT.provider,
        help="The agent CLI the improver runs on (default: %(default)s, the latest tournament's winner)",
    )
    run.add_argument("--model", help="The model the improver runs on (default: the provider's default)")
    run.add_argument("--agent-timeout-minutes", type=int, default=90)
    run.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    run.add_argument(
        "--mode", type=ImproverMode, choices=list(ImproverMode), default=DEFAULT_IMPROVER_MODE,
        help="empowered: the staged bundle plus the read-only toolbox; scripted: the bundle alone"
        " (default: %(default)s)",
    )
    run.add_argument(
        "--budget-minutes", type=int, default=60,
        help="Empowered: the investigation budget the agent is given (below the agent timeout)",
    )
    run.add_argument("--empowered-addendum", type=Path, default=EMPOWERED_ADDENDUM)
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
    return validate(args.run_dir)


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
    """The provider and model ``run`` launches the improver on."""
    return ImproverAgentChoice.for_provider(args.provider, args.model)


def run(args: argparse.Namespace) -> int:
    if (args.state_dir is None) != (args.audited_repo is None):
        raise SystemExit("improver run: --state-dir and --audited-repo go together")
    if args.exclude_open_issue and args.apply:
        raise SystemExit("improver run: --exclude-open-issue is a blind run; it cannot --apply")
    if args.mode is ImproverMode.EMPOWERED and args.budget_minutes >= args.agent_timeout_minutes:
        raise SystemExit("improver run: --budget-minutes must be below --agent-timeout-minutes")
    store = _store()
    prompt = args.prompt.read_text(encoding="utf-8")
    investigation = _investigation(args)

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
            clock=_now,
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


def validate(run_dir: Path) -> int:
    evidence = load_staged_evidence(run_dir / IMPROVER_DATA_DIRNAME)
    path = run_dir / FINDINGS_FILE
    if not path.is_file():
        print(f"[{Rule.SCHEMA.value}] the improver wrote no {FINDINGS_FILE}")
        return EXIT_REJECTED
    try:
        findings = validate_findings(path.read_bytes(), evidence)
    except ImproverFindingsRejected as rejection:
        for violation in rejection.violations:
            print(violation.describe())
        return EXIT_REJECTED
    print(f"valid: {len(findings.findings)} finding(s)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
