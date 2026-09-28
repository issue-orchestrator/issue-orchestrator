#!/usr/bin/env python3
"""The tech-lead improver's orchestrator side (#7490).

    python -m issue_orchestrator.entrypoints.cli_tools.improver stage \\
        --state-dir ~/dev/porchpin/.issue-orchestrator/state --audited-repo porchpin/porchpin \\
        --outputs-repo issue-orchestrator/issue-orchestrator --run-dir RUN [--previous-audit A] \\
        [--exam-dir D] [--engine-source-repo .]
    python -m issue_orchestrator.entrypoints.cli_tools.improver validate --run-dir RUN

``stage`` writes ``RUN/improver-data/`` (the prompt's inputs); ``validate``
checks ``RUN/improver-findings.json`` against them and prints every broken
rule. Exit codes follow the budgeted-validation convention: 0 success, 1 a
rejected findings file, 75 an input the improver cannot run without.

This is the composition root for both: the one place that picks the GitHub
host, the git runner and the clock and hands them to their owners.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...execution.engine_source_archive import GitEngineSourceArchive
from ...contracts.engine_audit import SourceStatus
from ...contracts.improver_inputs import IMPROVER_DATA_DIRNAME
from ...domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    validate_findings,
)
from ...execution.command_runner import LocalCommandRunner
from ...execution.providers import create_repository_host
from ...observation.engine_audit import Unavailable
from ..improver_staging import (
    ImproverInputStager,
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    load_staged_evidence,
)

#: The improver's one output file, beside ``improver-data/`` in the run dir.
FINDINGS_FILE = "improver-findings.json"

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNAVAILABLE = 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="improver", description="The tech-lead improver's orchestrator side (#7490)")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="Stage the improver's inputs")
    stage.add_argument("--state-dir", required=True, type=Path, help="The audited engine's state dir")
    stage.add_argument("--audited-repo", required=True, help="owner/repo the audited engine works")
    stage.add_argument("--outputs-repo", required=True, help="owner/repo the improver's outputs are filed in")
    stage.add_argument("--run-dir", required=True, type=Path)
    stage.add_argument("--previous-audit", type=Path, help="The previous run's audit.json")
    stage.add_argument("--exam-dir", type=Path, help="Where the tech-lead exam writes scorecards")
    stage.add_argument(
        "--engine-source-repo", type=Path, default=Path("."),
        help="An io repository holding the engine's commit (default: .)",
    )
    stage.add_argument("--window-hours", type=float, default=24.0)
    stage.add_argument("--log-tail-mb", type=int, default=64)
    stage.add_argument("--no-github", action="store_true", help="Do not read the audited repo's GitHub")
    validate = sub.add_parser("validate", help="Validate improver-findings.json")
    validate.add_argument("--run-dir", required=True, type=Path)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "stage":
        return stage(args)
    return validate(args.run_dir)


def stage(args: argparse.Namespace) -> int:
    if args.window_hours <= 0 or args.log_tail_mb <= 0:
        raise SystemExit("improver stage: --window-hours and --log-tail-mb must be positive")
    outputs = create_repository_host(args.outputs_repo)
    audited = (
        Unavailable(SourceStatus.SKIPPED, "--no-github")
        if args.no_github
        else outputs
        if args.audited_repo == args.outputs_repo
        else create_repository_host(args.audited_repo)
    )
    stager = ImproverInputStager(
        audited_host=audited,
        outputs_host=outputs,
        source=GitEngineSourceArchive(args.engine_source_repo.resolve(), LocalCommandRunner()),
        clock=lambda: datetime.now(UTC),
    )
    try:
        staged = stager.stage(
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
