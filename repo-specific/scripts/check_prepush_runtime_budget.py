#!/usr/bin/env python3
"""Repo-specific pre-push runtime budget guard."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from issue_orchestrator.infra.validation_timings import get_shared_timings_file


DEFAULT_CONFIG_PATH = Path("repo-specific/config/pre-push-runtime-budget.yaml")
DEFAULT_METRIC = "validation_elapsed_seconds"
DEFAULT_CONFIG_LABEL = "repo-specific/config/pre-push-runtime-budget.yaml"
PREPUSH_SUMMARY_KIND = "prepush_gate_summary"


@dataclass(frozen=True)
class RuntimeBudget:
    enabled: bool
    command: str | None
    metric: str
    baseline_seconds: float
    max_increase_seconds: float
    max_seconds: float

    @property
    def max_regressed_seconds(self) -> float:
        return self.baseline_seconds + self.max_increase_seconds


@dataclass(frozen=True)
class BudgetCheck:
    passed: bool
    message: str


def find_worktree_root() -> Path:
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


def read_head_sha(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Pre-push runtime budget failed: could not resolve HEAD SHA with "
            f"`git rev-parse HEAD`: {result.stderr.strip()}"
        )
    head_sha = result.stdout.strip()
    if not head_sha:
        raise RuntimeError(
            "Pre-push runtime budget failed: `git rev-parse HEAD` returned no SHA"
        )
    return head_sha


def load_budget(path: Path) -> RuntimeBudget:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = raw.get("pre_push_runtime", {})
    if not isinstance(section, dict):
        raise ValueError("pre_push_runtime must be a mapping")

    command = section.get("command")
    if command is not None and not isinstance(command, str):
        raise ValueError("command must be a string when set")

    budget = RuntimeBudget(
        enabled=bool(section.get("enabled", True)),
        command=command,
        metric=str(section.get("metric", DEFAULT_METRIC)),
        baseline_seconds=float(section["baseline_seconds"]),
        max_increase_seconds=float(section["max_increase_seconds"]),
        max_seconds=float(section["max_seconds"]),
    )
    if budget.baseline_seconds < 0:
        raise ValueError("baseline_seconds must be non-negative")
    if budget.max_increase_seconds < 0:
        raise ValueError("max_increase_seconds must be non-negative")
    if budget.max_seconds < 0:
        raise ValueError("max_seconds must be non-negative")
    return budget


def read_timing_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL timing record at {path}:{line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Timing record at {path}:{line_number} must be an object")
        records.append(record)
    return records


def select_measured_record(
    records: list[dict[str, Any]],
    *,
    head_sha: str,
    budget: RuntimeBudget,
) -> tuple[dict[str, Any] | None, str | None]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        if record.get("kind") != PREPUSH_SUMMARY_KIND:
            continue
        if record.get("phase") != "validation_gate":
            continue
        if record.get("final_exit_code") != 0:
            continue
        if record.get("head_sha") != head_sha:
            continue
        if budget.command is not None and record.get("command") != budget.command:
            continue
        candidates.append(record)

    if not candidates:
        return None, None

    uncached = [
        record for record in candidates if record.get("validation_cache_hit") is False
    ]
    if uncached:
        return uncached[-1], None

    command_detail = f" and command {budget.command!r}" if budget.command else ""
    return (
        None,
        "latest successful pre-push validation for this HEAD"
        f"{command_detail} was cached; runtime budgets use uncached records only",
    )


def evaluate_record(record: dict[str, Any], budget: RuntimeBudget) -> BudgetCheck:
    raw_value = record.get(budget.metric)
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        return BudgetCheck(
            passed=False,
            message=(
                "Pre-push runtime budget failed: timing record is missing numeric "
                f"{budget.metric!r}"
            ),
        )

    observed = float(raw_value)
    violations: list[str] = []
    if observed > budget.max_seconds:
        violations.append(
            f"{budget.metric} {observed:.1f}s exceeds max_seconds "
            f"{budget.max_seconds:.1f}s"
        )
    if observed > budget.max_regressed_seconds:
        violations.append(
            f"{budget.metric} "
            f"{observed:.1f}s exceeds baseline_seconds + max_increase_seconds "
            f"({budget.baseline_seconds:.1f}s + "
            f"{budget.max_increase_seconds:.1f}s = "
            f"{budget.max_regressed_seconds:.1f}s)"
        )

    if violations:
        return BudgetCheck(
            passed=False,
            message=(
                "Pre-push runtime budget exceeded:\n"
                + "\n".join(f"  - {violation}" for violation in violations)
                + "\nNext steps: amend or commit to create a new HEAD and rerun "
                "`make validate-pr` for a fresh uncached measurement, or adjust "
                f"{DEFAULT_CONFIG_LABEL} if the new runtime is intentional."
            ),
        )

    return BudgetCheck(
        passed=True,
        message=(
            "Pre-push runtime budget passed: "
            f"{budget.metric}={observed:.1f}s <= max_seconds "
            f"{budget.max_seconds:.1f}s and "
            f"<= baseline + increase {budget.max_regressed_seconds:.1f}s"
        ),
    )


def check_runtime_budget(
    *,
    config_path: Path,
    timings_file: Path | None,
    head_sha: str | None,
    worktree: Path,
) -> BudgetCheck:
    budget = load_budget(config_path)
    if not budget.enabled:
        return BudgetCheck(passed=True, message="Pre-push runtime budget disabled")

    resolved_timings_file = timings_file or get_shared_timings_file(worktree)
    if resolved_timings_file is None:
        return BudgetCheck(
            passed=False,
            message="Pre-push runtime budget failed: could not resolve git timing file",
        )
    if not resolved_timings_file.exists():
        return BudgetCheck(
            passed=False,
            message=(
                "Pre-push runtime budget failed: timing file does not exist at "
                f"{resolved_timings_file}"
            ),
        )

    resolved_head_sha = head_sha or read_head_sha(worktree)
    records = read_timing_records(resolved_timings_file)
    record, skipped_reason = select_measured_record(
        records,
        head_sha=resolved_head_sha,
        budget=budget,
    )
    if record is None and skipped_reason is not None:
        return BudgetCheck(
            passed=True,
            message=f"Pre-push runtime budget skipped: {skipped_reason}",
        )
    if record is None:
        head_detail = f" for HEAD {resolved_head_sha}" if resolved_head_sha else ""
        command_detail = f" matching command {budget.command!r}" if budget.command else ""
        return BudgetCheck(
            passed=False,
            message=(
                "Pre-push runtime budget failed: no successful uncached "
                f"pre-push timing record{head_detail}{command_detail}. Run "
                "`make validate-pr` to seed a timing record; if "
                "validation.publish.cmd changed, update "
                f"{DEFAULT_CONFIG_LABEL}."
            ),
        )

    return evaluate_record(record, budget)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Budget YAML path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--timings-file",
        type=Path,
        help="Override the shared validation timings JSONL file",
    )
    parser.add_argument(
        "--head-sha",
        help="Override the HEAD SHA used to select timing records",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = check_runtime_budget(
        config_path=args.config,
        timings_file=args.timings_file,
        head_sha=args.head_sha,
        worktree=find_worktree_root(),
    )
    output = sys.stdout if result.passed else sys.stderr
    print(result.message, file=output)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
