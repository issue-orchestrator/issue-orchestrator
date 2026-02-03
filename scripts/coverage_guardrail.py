#!/usr/bin/env python3
"""Per-file coverage guardrail for changed files."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from issue_orchestrator.validation.coverage_guardrail import (  # noqa: E402
    GuardrailConfig,
    evaluate_coverage,
    select_candidates,
)


def _run(cmd: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def _repo_root() -> Path:
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    if result.returncode != 0:
        raise RuntimeError("Not a git repository (or no worktree available)")
    return Path(result.stdout.strip())


def _load_config(repo_root: Path) -> dict:
    from issue_orchestrator.infra.config import find_config_file

    config_path = find_config_file(repo_root)
    if not config_path:
        return {
            "enabled": False,
            "min_percent": None,
            "apply_to": "changed",
            "scope": [],
            "coverage_type": "line",
            "exclude": [],
        }

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    validation = config.get("validation", {}) or {}
    guardrail = validation.get("coverage_guardrail", {}) or {}
    return {
        "enabled": guardrail.get("enabled", False),
        "min_percent": guardrail.get("min_percent"),
        "apply_to": guardrail.get("apply_to", "changed"),
        "scope": guardrail.get("scope", []) or [],
        "coverage_type": guardrail.get("coverage_type", "line"),
        "exclude": guardrail.get("exclude", []) or [],
    }


def _resolve_base_ref(repo_root: Path) -> str | None:
    origin_head = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo_root)
    if origin_head.returncode == 0:
        return origin_head.stdout.strip()

    for candidate in ("origin/main", "origin/master", "main", "master"):
        exists = _run(["git", "rev-parse", "--verify", candidate], cwd=repo_root)
        if exists.returncode == 0:
            return candidate

    fallback = _run(["git", "rev-parse", "--verify", "HEAD~1"], cwd=repo_root)
    if fallback.returncode == 0:
        return "HEAD~1"

    return None


def _changed_files(repo_root: Path) -> list[str]:
    base_ref = _resolve_base_ref(repo_root)
    if not base_ref:
        return []

    diff = _run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            f"{base_ref}...HEAD",
        ],
        cwd=repo_root,
    )
    if diff.returncode != 0:
        return []
    files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    return files


def _tracked_files(repo_root: Path) -> list[str]:
    result = _run(["git", "ls-files"], cwd=repo_root)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _cov_sources(scope: list[str]) -> list[str]:
    sources: list[str] = []
    for pattern in scope:
        wildcard_pos = len(pattern)
        for token in ("*", "?", "["):
            pos = pattern.find(token)
            if pos != -1:
                wildcard_pos = min(wildcard_pos, pos)
        prefix = pattern[:wildcard_pos].rstrip("/")
        if prefix:
            sources.append(prefix)
    return sorted(set(sources)) if sources else ["src"]


def _run_pytest_with_coverage(
    repo_root: Path,
    coverage_type: str,
    scope: list[str],
    output_path: Path,
) -> int:
    cmd = [sys.executable, "-m", "pytest", "tests/unit", "-x", "-q", "--tb=short"]
    sources = _cov_sources(scope)
    for source in sources:
        cmd.append(f"--cov={source}")
    cmd.append(f"--cov-report=json:{output_path}")
    if coverage_type == "branch":
        cmd.append("--cov-branch")

    result = subprocess.run(cmd, cwd=repo_root)
    return result.returncode


def _run_pytest_no_coverage(repo_root: Path) -> int:
    cmd = [sys.executable, "-m", "pytest", "tests/unit", "-x", "-q", "--tb=short"]
    result = subprocess.run(cmd, cwd=repo_root)
    return result.returncode


def _load_coverage(coverage_path: Path, repo_root: Path) -> dict[str, dict]:
    with open(coverage_path) as f:
        data = json.load(f)

    files = {}
    for filename, info in data.get("files", {}).items():
        path = Path(filename)
        if path.is_absolute():
            try:
                rel = path.resolve().relative_to(repo_root)
                key = rel.as_posix()
            except ValueError:
                key = path.as_posix()
        else:
            key = path.as_posix()
        files[key] = info
    return files


def main() -> int:
    try:
        repo_root = _repo_root()
    except RuntimeError as exc:
        print(f"Coverage guardrail: {exc}")
        return 2

    guardrail = _load_config(repo_root)
    if not guardrail["enabled"]:
        return _run_pytest_no_coverage(repo_root)

    coverage_type = guardrail["coverage_type"]
    if coverage_type not in {"line", "branch"}:
        print("Coverage guardrail: coverage_type must be 'line' or 'branch'")
        return 2

    min_percent = guardrail["min_percent"]
    if min_percent is None:
        print("Coverage guardrail: min_percent must be set when enabled")
        return 2

    config = GuardrailConfig(
        enabled=guardrail["enabled"],
        min_percent=guardrail["min_percent"],
        apply_to=guardrail.get("apply_to", "changed"),
        scope=guardrail["scope"],
        coverage_type=guardrail["coverage_type"],
        exclude=guardrail["exclude"],
    )

    selection = select_candidates(
        config=config,
        changed_files=_changed_files(repo_root),
        tracked_files=_tracked_files(repo_root),
    )
    if selection.error:
        print(f"Coverage guardrail: {selection.error}")
        return 2
    if selection.skip_reason:
        print(f"Coverage guardrail: {selection.skip_reason}")
        return _run_pytest_no_coverage(repo_root)

    coverage_dir = repo_root / ".issue-orchestrator" / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = coverage_dir / "coverage.json"

    exit_code = _run_pytest_with_coverage(repo_root, coverage_type, scope, coverage_path)
    if exit_code != 0:
        return exit_code

    if not coverage_path.exists():
        print("Coverage guardrail: coverage report not found")
        return 2

    coverage = _load_coverage(coverage_path, repo_root)
    coverage_map: dict[str, float | None] = {}
    for path, info in coverage.items():
        if not info:
            coverage_map[path] = None
            continue
        coverage_map[path] = info.get("summary", {}).get("percent_covered")

    failures = evaluate_coverage(selection.candidates, coverage_map, float(min_percent))

    if failures:
        print("Coverage guardrail: per-file coverage below threshold")
        for failure in failures:
            if failure.percent is None:
                print(f"  {failure.path}: no coverage data")
            else:
                print(f"  {failure.path}: {failure.percent:.2f}%")
        print(f"Required minimum: {float(min_percent):.2f}%")
        return 1

    print("Coverage guardrail: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
