#!/usr/bin/env python3
"""The tech-lead improver's orchestrator side (#7490).

    improver run --state-dir ~/dev/porchpin/.issue-orchestrator/state \
        --audited-repo porchpin/porchpin --outputs-repo issue-orchestrator/issue-orchestrator \
        --model gpt-5.6-sol [--exam-dir D] [--engine-source-repo .]
    improver status
    improver apply --outputs-repo issue-orchestrator/issue-orchestrator
    improver stage ... --run-dir RUN [--previous-audit A]
    improver validate --run-dir RUN

(each ``python -m issue_orchestrator.entrypoints.cli_tools.improver ...``)

``run`` is the whole daily run (:mod:`..improver_run`): stage the inputs, run
the improver read-only on Codex, validate its findings strictly, record the
run and apply the accepted findings' GitHub effects. ``status`` prints the
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

from ...execution.engine_source_archive import GitEngineSourceArchive
from ...contracts.engine_audit import SourceStatus
from ...contracts.improver_findings import FINDINGS_FILE
from ...contracts.improver_inputs import IMPROVER_DATA_DIRNAME
from ...domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    validate_findings,
)
from ...execution.command_runner import LocalCommandRunner
from ...execution.providers import create_repository_host
from ...observation.engine_audit import Unavailable
from ...execution.improver_effect_applier import ImproverEffects
from ...execution.codex_improver_agent import CodexImproverAgent
from ...execution.improver_run_store import FileImproverRunStore
from ...ports.improver import ImproverStoreBusy
from ...execution.process_group_command_runner import ProcessGroupCommandRunner
from ..improver_run import ImproverRun, ImproverRunRequest, render_run
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


def _engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True, type=Path, help="The audited engine's state dir")
    parser.add_argument("--audited-repo", required=True, help="owner/repo the audited engine works")
    parser.add_argument("--outputs-repo", required=True, help="owner/repo the improver's outputs are filed in")
    parser.add_argument("--exam-dir", type=Path, help="Where the tech-lead exam writes scorecards")
    parser.add_argument(
        "--engine-source-repo", type=Path, default=Path("."),
        help="An io repository holding the engine's commit (default: .)",
    )
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--log-tail-mb", type=int, default=64)
    parser.add_argument("--no-github", action="store_true", help="Do not read the audited repo's GitHub")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="improver", description="The tech-lead improver's orchestrator side (#7490)")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="Stage the improver's inputs")
    _engine_arguments(stage)
    stage.add_argument("--run-dir", required=True, type=Path)
    stage.add_argument("--previous-audit", type=Path, help="The previous run's audit.json")
    validate = sub.add_parser("validate", help="Validate improver-findings.json")
    validate.add_argument("--run-dir", required=True, type=Path)
    run = sub.add_parser("run", help="Stage, run the improver on Codex, validate, record and apply")
    _engine_arguments(run)
    run.add_argument("--model", required=True, help="The Codex model the improver runs on")
    run.add_argument("--agent-timeout-minutes", type=int, default=90)
    run.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    run.add_argument(
        "--no-apply", action="store_true",
        help="Record accepted findings' effects as owed without touching GitHub (see apply)",
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


def _stager(args: argparse.Namespace) -> ImproverInputStager:
    if args.window_hours <= 0 or args.log_tail_mb <= 0:
        raise SystemExit("improver: --window-hours and --log-tail-mb must be positive")
    outputs = create_repository_host(args.outputs_repo)
    audited = (
        Unavailable(SourceStatus.SKIPPED, "--no-github")
        if args.no_github
        else outputs
        if args.audited_repo == args.outputs_repo
        else create_repository_host(args.audited_repo)
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
        staged = _stager(args).stage(
            ImproverStagingRequest(
                state_dir=args.state_dir.expanduser().resolve(),
                audited_repo=args.audited_repo,
                outputs_repo=args.outputs_repo,
                run_dir=args.run_dir.expanduser().resolve(),
                previous_audit=args.previous_audit,
                exam_dir=args.exam_dir,
                window=timedelta(hours=args.window_hours),
                log_tail_bytes=args.log_tail_mb * 1024 * 1024,
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


def run(args: argparse.Namespace) -> int:
    store = _store()
    improver = ImproverRun(
        store=store,
        stager=_stager(args),
        agent=CodexImproverAgent(
            runner=ProcessGroupCommandRunner(),
            model=args.model,
            timeout_seconds=args.agent_timeout_minutes * 60,
        ),
        effects=_effects(args.outputs_repo, store),
        prompt=args.prompt.read_text(encoding="utf-8"),
        clock=_now,
    )
    try:
        record = improver.run(
            ImproverRunRequest(
                state_dir=args.state_dir.expanduser().resolve(),
                audited_repo=args.audited_repo,
                outputs_repo=args.outputs_repo,
                exam_dir=args.exam_dir,
                window=timedelta(hours=args.window_hours),
                log_tail_bytes=args.log_tail_mb * 1024 * 1024,
            ),
            apply=not args.no_apply,
        )
    except ImproverStoreBusy as busy:
        print(f"improver run: {busy}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(render_run(record))
    # --no-apply leaves effects owed on purpose; only its outcome counts.
    return record.outcome.exit_code if args.no_apply else record.exit_code


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
