from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence
from xml.etree import ElementTree

BENCHMARK_TEST_FILE = Path("tests/simulated_scenarios/test_simulated_scenarios.py")
DEFAULT_OUTPUT_DIR = Path(".issue-orchestrator/portfolio-benchmark/latest")


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    capability: str
    claim: str
    why_it_matters: str
    test_name: str

    @property
    def pytest_target(self) -> str:
        return f"{BENCHMARK_TEST_FILE.as_posix()}::{self.test_name}"


@dataclass(frozen=True)
class BenchmarkCaseResult:
    case: BenchmarkCase
    status: str
    duration_seconds: float
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case.case_id,
            "capability": self.case.capability,
            "claim": self.case.claim,
            "why_it_matters": self.case.why_it_matters,
            "pytest_target": self.case.pytest_target,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PortfolioBenchmarkReport:
    generated_at: str
    repo_root: Path
    output_dir: Path
    command: list[str]
    pytest_exit_code: int
    results: tuple[BenchmarkCaseResult, ...]

    @property
    def total_duration_seconds(self) -> float:
        return round(sum(result.duration_seconds for result in self.results), 3)

    @property
    def total_cases(self) -> int:
        return len(self.results)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    @property
    def overall_status(self) -> str:
        return "passed" if all(result.status == "passed" for result in self.results) else "failed"

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "repo_root": str(self.repo_root),
            "output_dir": str(self.output_dir),
            "command": list(self.command),
            "pytest_exit_code": self.pytest_exit_code,
            "overall_status": self.overall_status,
            "total_cases": self.total_cases,
            "total_duration_seconds": self.total_duration_seconds,
            "counts": self.counts,
            "results": [result.to_dict() for result in self.results],
        }


PORTFOLIO_BENCHMARK_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        case_id="happy_path_pr",
        capability="Deterministic local coder-reviewer loop",
        claim="A bounded local review exchange can complete and produce a merge-ready PR.",
        why_it_matters="Shows the system can convert an issue into reviewed code without hand-held agent orchestration.",
        test_name="test_local_loop_happy_path_creates_non_draft_pr",
    ),
    BenchmarkCase(
        case_id="draft_pr_review",
        capability="Structured draft-PR review workflow",
        claim="The draft-PR review path applies approval labels and advances the issue state correctly.",
        why_it_matters="Demonstrates that the orchestrator mediates review outcomes instead of treating agent output as self-authenticating.",
        test_name="test_review_queue_approved_flow_updates_pr_labels",
    ),
    BenchmarkCase(
        case_id="review_rework",
        capability="Reviewer-driven rework loop",
        claim="Changes-requested reviews can send work back for rework and later converge on approval.",
        why_it_matters="This is the core applied-AI quality story: the system can absorb critique and recover, not just succeed on the happy path.",
        test_name="test_review_rework_then_approved",
    ),
    BenchmarkCase(
        case_id="validation_retry",
        capability="Bounded validation retry",
        claim="A failed validation step can retry once and still converge on a passing state.",
        why_it_matters="Shows the orchestrator handles flaky or transient failure modes with controlled retries instead of silent loops.",
        test_name="test_validation_retry_succeeds_after_retry",
    ),
    BenchmarkCase(
        case_id="publish_failure",
        capability="Failure classification and blocking",
        claim="Publish failures are classified, surfaced, and leave the issue blocked instead of pretending success.",
        why_it_matters="Hiring teams care about what happens when the model or environment is wrong; this demonstrates conservative failure handling.",
        test_name="test_processing_failure_push_error_marks_blocked_failed",
    ),
    BenchmarkCase(
        case_id="needs_human",
        capability="Human-in-the-loop escalation",
        claim="Agent-declared ambiguity becomes an explicit needs-human state with matching events and labels.",
        why_it_matters="This makes the system credible as applied AI rather than 'full autonomy' theater.",
        test_name="test_completion_outcome_needs_human_sets_label_and_event",
    ),
    BenchmarkCase(
        case_id="reconciliation_pause",
        capability="External-state reconciliation",
        claim="Label drift triggers reconcile-required events and pauses the issue instead of mutating stale state.",
        why_it_matters="Real systems drift. This shows the control plane treats external coordination state as authoritative and fallible.",
        test_name="test_reconciliation_drift_pauses_issue",
    ),
    BenchmarkCase(
        case_id="run_manifest",
        capability="Run-scoped diagnostics artifacts",
        claim="Validation failures update the run manifest with explicit status and completion metadata.",
        why_it_matters="Applied AI systems need replayable evidence, not hand-wavy logs. This proves the diagnostics contract is exercised.",
        test_name="test_validation_failure_updates_run_manifest",
    ),
    BenchmarkCase(
        case_id="restart_recovery",
        capability="Crash-safe restart recovery",
        claim="Restarted orchestrators recover work from durable labels instead of relying on in-memory session state.",
        why_it_matters="This is one of the strongest signals that the system was designed as infrastructure, not a demo script.",
        test_name="test_restart_recovery_uses_labels_not_memory",
    ),
    BenchmarkCase(
        case_id="sqlite_backups",
        capability="Durable local state protection",
        claim="Existing SQLite state is backed up automatically when backup cadence is enabled.",
        why_it_matters="It shows long-term operational thinking around stateful AI workflows and recovery, not just prompt choreography.",
        test_name="test_sqlite_backups_created_for_existing_dbs",
    ),
)


def select_cases(case_ids: Sequence[str] | None = None) -> tuple[BenchmarkCase, ...]:
    if not case_ids:
        return PORTFOLIO_BENCHMARK_CASES

    wanted = {case.case_id: case for case in PORTFOLIO_BENCHMARK_CASES}
    missing = [case_id for case_id in case_ids if case_id not in wanted]
    if missing:
        available = ", ".join(case.case_id for case in PORTFOLIO_BENCHMARK_CASES)
        raise ValueError(f"Unknown benchmark case(s): {', '.join(missing)}. Available: {available}")
    return tuple(wanted[case_id] for case_id in case_ids)


def build_pytest_command(
    *,
    junit_xml_path: Path,
    cases: Sequence[BenchmarkCase],
    extra_pytest_args: Sequence[str] | None = None,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--junitxml",
        str(junit_xml_path),
        *(extra_pytest_args or ()),
        *(case.pytest_target for case in cases),
    ]


def run_portfolio_benchmark(
    *,
    repo_root: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    case_ids: Sequence[str] | None = None,
    extra_pytest_args: Sequence[str] | None = None,
) -> PortfolioBenchmarkReport:
    benchmark_cases = select_cases(case_ids)
    if not benchmark_cases:
        raise ValueError("At least one benchmark case is required.")

    repo_root = repo_root.resolve()
    if not (repo_root / BENCHMARK_TEST_FILE).exists():
        raise FileNotFoundError(
            f"Portfolio benchmark expects {BENCHMARK_TEST_FILE.as_posix()} under {repo_root}"
        )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    junit_xml_path = output_dir / "junit.xml"
    command = build_pytest_command(
        junit_xml_path=junit_xml_path,
        cases=benchmark_cases,
        extra_pytest_args=extra_pytest_args,
    )

    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    results = parse_junit_report(junit_xml_path, benchmark_cases)
    report = PortfolioBenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        repo_root=repo_root,
        output_dir=output_dir,
        command=command,
        pytest_exit_code=completed.returncode,
        results=tuple(results),
    )
    write_report_artifacts(
        report=report,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return report


def parse_junit_report(
    junit_xml_path: Path,
    cases: Sequence[BenchmarkCase],
) -> list[BenchmarkCaseResult]:
    if not junit_xml_path.exists():
        return [
            BenchmarkCaseResult(
                case=case,
                status="missing",
                duration_seconds=0.0,
                detail="Pytest did not produce junit.xml for this benchmark run.",
            )
            for case in cases
        ]

    root = ElementTree.parse(junit_xml_path).getroot()
    testcases = root.findall(".//testcase")
    by_name = {
        testcase.attrib.get("name", ""): testcase
        for testcase in testcases
        if testcase.attrib.get("name")
    }
    return [parse_benchmark_case(case, by_name.get(case.test_name)) for case in cases]


def parse_benchmark_case(
    case: BenchmarkCase,
    testcase: ElementTree.Element | None,
) -> BenchmarkCaseResult:
    if testcase is None:
        return BenchmarkCaseResult(
            case=case,
            status="missing",
            duration_seconds=0.0,
            detail="Selected benchmark case was not present in junit.xml output.",
        )

    duration = float(testcase.attrib.get("time", "0") or "0")
    for tag, status in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        child = testcase.find(tag)
        if child is None:
            continue
        detail = child.attrib.get("message") or _summarize_text(child.text)
        return BenchmarkCaseResult(
            case=case,
            status=status,
            duration_seconds=duration,
            detail=detail,
        )

    return BenchmarkCaseResult(case=case, status="passed", duration_seconds=duration)


def write_report_artifacts(
    *,
    report: PortfolioBenchmarkReport,
    stdout: str,
    stderr: str,
) -> None:
    (report.output_dir / "summary.json").write_text(
        json.dumps(report.to_dict(), indent=2) + "\n"
    )
    (report.output_dir / "summary.md").write_text(render_markdown(report))
    (report.output_dir / "pytest-command.txt").write_text(" ".join(report.command) + "\n")
    (report.output_dir / "pytest-stdout.txt").write_text(stdout)
    (report.output_dir / "pytest-stderr.txt").write_text(stderr)


def render_markdown(report: PortfolioBenchmarkReport) -> str:
    counts = report.counts
    count_summary = ", ".join(
        f"{status}={counts[status]}"
        for status in sorted(counts)
    )
    lines = [
        "# Applied AI Portfolio Benchmark",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Repo root: `{report.repo_root}`",
        f"- Output dir: `{report.output_dir}`",
        f"- Overall status: `{report.overall_status}`",
        f"- Pytest exit code: `{report.pytest_exit_code}`",
        f"- Cases: `{report.total_cases}` ({count_summary})",
        f"- Aggregate duration (reported by pytest): `{report.total_duration_seconds:.3f}s`",
        "",
        "## Claims",
        "",
        "| Case | Status | Capability | Claim | Why It Matters |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in report.results:
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_escape(result.case.case_id),
                    _table_escape(result.status),
                    _table_escape(result.case.capability),
                    _table_escape(result.case.claim),
                    _table_escape(result.case.why_it_matters),
                ]
            )
            + " |"
        )
        if result.detail:
            lines.append(
                f"| `{result.case.case_id}:detail` |  |  |  | {_table_escape(result.detail)} |"
            )
    lines.extend(
        [
            "",
            "## Artifact Bundle",
            "",
            "- `summary.json` — machine-readable report suitable for dashboards, resume snippets, or portfolio automation.",
            "- `summary.md` — shareable benchmark summary for project pages or interview packets.",
            "- `junit.xml` — raw pytest output for auditability.",
            "- `pytest-command.txt` — exact command used to generate the bundle.",
            "- `pytest-stdout.txt` / `pytest-stderr.txt` — execution logs for debugging failures.",
            "",
        ]
    )
    return "\n".join(lines)


def list_cases() -> str:
    lines = ["Available portfolio benchmark cases:"]
    for case in PORTFOLIO_BENCHMARK_CASES:
        lines.append(f"- {case.case_id}: {case.capability}")
        lines.append(f"  claim: {case.claim}")
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the applied-AI portfolio benchmark against deterministic simulated scenarios.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing tests/simulated_scenarios/.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where summary artifacts should be written.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        default=[],
        help="Benchmark case id to run. Repeat to select multiple cases.",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        dest="pytest_args",
        default=[],
        help="Extra argument passed through to pytest. Repeat as needed.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available benchmark cases and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    if args.list:
        print(list_cases(), end="")
        return 0

    try:
        report = run_portfolio_benchmark(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            case_ids=args.case_ids,
            extra_pytest_args=args.pytest_args,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Portfolio benchmark {report.overall_status}: {report.counts}")
    print(f"Artifacts written to {report.output_dir}")
    return 0 if report.overall_status == "passed" else 1


def _summarize_text(text: str | None, *, max_length: int = 200) -> str | None:
    if not text:
        return None
    collapsed = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if not collapsed:
        return None
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 3] + "..."


def _table_escape(value: object) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
