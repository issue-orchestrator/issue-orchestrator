"""Coverage guardrail logic with dependency injection friendly helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional


@dataclass(frozen=True)
class GuardrailConfig:
    enabled: bool = False
    min_percent: Optional[float] = None
    apply_to: str = "changed"  # "changed" or "all"
    scope: list[str] = field(default_factory=list)
    coverage_type: str = "line"  # "line" or "branch"
    exclude: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GuardrailSelection:
    candidates: list[str]
    error: Optional[str] = None
    skip_reason: Optional[str] = None


@dataclass(frozen=True)
class GuardrailFailure:
    path: str
    percent: Optional[float]


def matches_any(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    posix_path = PurePosixPath(path)
    for pattern in patterns:
        if PurePosixPath(path).match(pattern):
            return True
        if posix_path.match(pattern):
            return True
    return False


def filter_files(files: list[str], scope: list[str], exclude: list[str]) -> list[str]:
    if not scope:
        return []
    selected = []
    for path in files:
        if not matches_any(path, scope):
            continue
        if exclude and matches_any(path, exclude):
            continue
        selected.append(path)
    return selected


def select_candidates(
    config: GuardrailConfig,
    changed_files: list[str],
    tracked_files: list[str],
) -> GuardrailSelection:
    if not config.scope:
        return GuardrailSelection([], error="scope must be set when enabled")

    if config.apply_to not in {"changed", "all"}:
        return GuardrailSelection([], error="apply_to must be 'changed' or 'all'")

    if config.apply_to == "all":
        candidates = filter_files(tracked_files, config.scope, config.exclude)
        if not candidates:
            return GuardrailSelection([], error="no tracked files matched scope")
        return GuardrailSelection(candidates)

    candidates = filter_files(changed_files, config.scope, config.exclude)
    if not candidates:
        return GuardrailSelection([], skip_reason="no changed files in scope")
    return GuardrailSelection(candidates)


def evaluate_coverage(
    candidates: list[str],
    coverage_map: dict[str, Optional[float]],
    min_percent: float,
) -> list[GuardrailFailure]:
    failures: list[GuardrailFailure] = []
    for path in candidates:
        percent = coverage_map.get(path)
        if percent is None or percent < min_percent:
            failures.append(GuardrailFailure(path=path, percent=percent))
    return failures
