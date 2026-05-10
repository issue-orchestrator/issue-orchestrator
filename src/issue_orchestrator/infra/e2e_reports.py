"""Typed helpers for generic E2E result and artifact reporting."""

from __future__ import annotations

import glob
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from defusedxml import ElementTree
from xml.etree.ElementTree import Element as XmlElement
from xml.etree.ElementTree import ParseError as XmlParseError


CaseOutcome = Literal["passed", "failed", "error", "skipped"]

# Captured stdout/stderr from JUnit are read on-demand from disk (see the
# /api/e2e-run/{id}/test-output endpoint) — never persisted to SQLite. Cap each
# channel to keep parser memory bounded when a test produces megabytes of logs.
MAX_CAPTURED_OUTPUT_CHARS = 100_000


@dataclass(frozen=True)
class JUnitCaseResult:
    """One structured result case parsed from a JUnit XML report."""

    case_id: str
    display_name: str
    suite_name: str | None
    outcome: CaseOutcome
    duration_seconds: float | None
    failure_details: str | None = None
    system_out: str | None = None
    system_err: str | None = None

    @property
    def failure_summary(self) -> str | None:
        if self.failure_details is None:
            return None
        first_line = self.failure_details.strip().splitlines()[0]
        return first_line[:240]


@dataclass(frozen=True)
class E2ERunArtifactRecord:
    """One run-scoped artifact exposed to the dashboard."""

    kind: str
    label: str
    path: str


def discover_report_artifacts(
    repo_root: Path,
    *,
    junit_xml_paths: list[str] | tuple[str, ...],
    artifact_paths: list[str] | tuple[str, ...],
    modified_after: float | None = None,
) -> tuple[list[JUnitCaseResult], list[E2ERunArtifactRecord]]:
    """Resolve configured reports/artifacts and return typed results.

    Raises:
        ValueError: if a configured path/glob resolves to nothing, resolves only
        stale JUnit files when ``modified_after`` is set, or a JUnit report is
        malformed.
    """
    cases: list[JUnitCaseResult] = []
    artifacts: list[E2ERunArtifactRecord] = []

    resolved_junit_files = _resolve_paths(repo_root, junit_xml_paths)
    junit_files = _filter_modified_after(resolved_junit_files, modified_after)
    if junit_xml_paths and not junit_files:
        message = (
            "Configured JUnit XML paths did not resolve to any fresh files"
            if resolved_junit_files and modified_after is not None
            else "Configured JUnit XML paths did not resolve to any files"
        )
        raise ValueError(message)
    for path in junit_files:
        cases.extend(parse_junit_report(path))
        artifacts.append(
            E2ERunArtifactRecord(
                kind="junit_xml",
                label=f"JUnit XML: {path.name}",
                path=str(path),
            )
        )

    extra_files = _resolve_paths(repo_root, artifact_paths)
    if artifact_paths and not extra_files:
        raise ValueError("Configured artifact paths did not resolve to any files")
    for path in extra_files:
        artifacts.append(_artifact_record_for_path(path))

    return cases, _dedupe_artifacts(artifacts)


def parse_junit_report(path: Path) -> list[JUnitCaseResult]:
    """Parse JUnit XML into strongly typed case results.

    Raises:
        ValueError: when the file is missing or structurally malformed.
    """
    if not path.exists():
        raise ValueError(f"JUnit XML does not exist: {path}")

    try:
        root = cast(XmlElement, ElementTree.parse(path).getroot())
    except XmlParseError as exc:
        raise ValueError(
            f"Malformed JUnit XML: {path}{_parse_error_position(exc)}"
        ) from exc

    cases = root.findall(".//testcase")
    if not cases:
        raise ValueError(f"JUnit XML did not contain any <testcase> entries: {path}")

    return [_parse_testcase(case) for case in cases]


@lru_cache(maxsize=16)
def _parse_junit_cached(
    path_str: str, mtime_ns: int, size: int
) -> tuple[tuple[JUnitCaseResult, ...], tuple[JUnitCaseResult, ...]]:
    """Parse + normalize once per (path, mtime, size). Tuples for hashability."""
    raw = parse_junit_report(Path(path_str))
    normalized = normalize_pytest_junit_cases(raw)
    return tuple(raw), tuple(normalized)


def parse_junit_report_cached(
    path: Path,
) -> tuple[tuple[JUnitCaseResult, ...], tuple[JUnitCaseResult, ...]]:
    """Memoized variant of parse_junit_report + normalize_pytest_junit_cases.

    Intended for endpoints that re-fetch the same JUnit XML on every row
    expand — a failure-heavy run can hit the parser dozens of times for the
    same on-disk file. Cache key is (path, mtime_ns, size) so the entry
    invalidates if the file is rewritten; LRU caps memory at 16 distinct
    files (a few recent runs' worth).
    """
    stat = path.stat()
    return _parse_junit_cached(str(path), stat.st_mtime_ns, stat.st_size)


def normalize_pytest_junit_cases(
    cases: list[JUnitCaseResult],
) -> list[JUnitCaseResult]:
    """Normalize pytest JUnit case IDs to runtime-style nodeids.

    Pytest's JUnit XML typically emits dotted module paths in ``classname``
    (for example ``tests.e2e.test_basic``) while runtime observation records
    nodeids with filesystem separators
    (for example ``tests/e2e/test_basic.py::test_failing``). Converge the
    structured report onto the runtime shape so one test maps to one row.
    """
    normalized: list[JUnitCaseResult] = []
    for case in cases:
        suite_name = case.suite_name
        if not suite_name or "/" in suite_name:
            normalized.append(case)
            continue
        parts = [part for part in suite_name.split(".") if part]
        if len(parts) < 2:
            normalized.append(case)
            continue
        path = "/".join((*parts[:-1], f"{parts[-1]}.py"))
        normalized.append(
            replace(
                case,
                suite_name=path,
                case_id=f"{path}::{case.display_name}",
            )
        )
    return normalized


def _parse_testcase(testcase: XmlElement) -> JUnitCaseResult:
    display_name = (testcase.attrib.get("name") or "").strip()
    if not display_name:
        raise ValueError("JUnit testcase is missing a non-empty name attribute")

    suite_name = _optional_text(testcase.attrib.get("classname"))
    case_id = f"{suite_name}::{display_name}" if suite_name else display_name
    duration_seconds = _optional_float(testcase.attrib.get("time"))
    system_out = _captured_channel_text(testcase.find("system-out"))
    system_err = _captured_channel_text(testcase.find("system-err"))

    failure = testcase.find("failure")
    if failure is not None:
        return JUnitCaseResult(
            case_id=case_id,
            display_name=display_name,
            suite_name=suite_name,
            outcome="failed",
            duration_seconds=duration_seconds,
            failure_details=_detail_text(failure),
            system_out=system_out,
            system_err=system_err,
        )

    error = testcase.find("error")
    if error is not None:
        return JUnitCaseResult(
            case_id=case_id,
            display_name=display_name,
            suite_name=suite_name,
            outcome="error",
            duration_seconds=duration_seconds,
            failure_details=_detail_text(error),
            system_out=system_out,
            system_err=system_err,
        )

    skipped = testcase.find("skipped")
    if skipped is not None:
        return JUnitCaseResult(
            case_id=case_id,
            display_name=display_name,
            suite_name=suite_name,
            outcome="skipped",
            duration_seconds=duration_seconds,
            failure_details=_detail_text(skipped),
            system_out=system_out,
            system_err=system_err,
        )

    return JUnitCaseResult(
        case_id=case_id,
        display_name=display_name,
        suite_name=suite_name,
        outcome="passed",
        duration_seconds=duration_seconds,
        system_out=system_out,
        system_err=system_err,
    )


def _captured_channel_text(element: XmlElement | None) -> str | None:
    if element is None:
        return None
    if not isinstance(element.text, str):
        return None
    text = element.text.strip()
    if not text:
        return None
    if len(text) > MAX_CAPTURED_OUTPUT_CHARS:
        return text[:MAX_CAPTURED_OUTPUT_CHARS]
    return text


def _detail_text(element: XmlElement) -> str | None:
    message = _optional_text(element.attrib.get("message"))
    body = _optional_text(element.text)
    if message and body:
        return f"{message}\n{body}"
    return message or body


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"Expected float-like JUnit time, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)


def _resolve_paths(
    repo_root: Path,
    patterns: list[str] | tuple[str, ...],
) -> list[Path]:
    repo_root_resolved = repo_root.resolve()
    resolved: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        candidate = pattern.strip()
        if not candidate:
            continue
        matches = _glob_matches(repo_root, candidate)
        for path in matches:
            if path.is_dir():
                continue
            real = path.resolve()
            if not real.is_relative_to(repo_root_resolved):
                raise ValueError(
                    f"E2E report path resolves outside repo root: {candidate}"
                )
            if real in seen:
                continue
            resolved.append(real)
            seen.add(real)
    return resolved


def _glob_matches(repo_root: Path, candidate: str) -> list[Path]:
    search_pattern = candidate if Path(candidate).is_absolute() else str(repo_root / candidate)
    return [Path(match) for match in sorted(glob.glob(search_pattern, recursive=True))]


def _filter_modified_after(paths: list[Path], modified_after: float | None) -> list[Path]:
    if modified_after is None:
        return paths
    return [path for path in paths if path.stat().st_mtime >= modified_after]


def _parse_error_position(exc: XmlParseError) -> str:
    position = getattr(exc, "position", None)
    if not isinstance(position, tuple) or len(position) != 2:
        return ""
    line, column = position
    return f" (line {line}, column {column})"


def _artifact_record_for_path(path: Path) -> E2ERunArtifactRecord:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix == ".html":
        kind = "html_report"
        label = f"HTML Report: {path.name}"
    elif suffix == ".json":
        kind = "json_report"
        label = f"JSON Report: {path.name}"
    elif suffix == ".xml":
        kind = "xml_report"
        label = f"XML Report: {path.name}"
    elif suffix == ".zip" and "trace" in name:
        kind = "trace"
        label = f"Trace: {path.name}"
    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        kind = "image"
        label = f"Image: {path.name}"
    elif suffix in {".log", ".txt"}:
        kind = "text_artifact"
        label = f"Text Artifact: {path.name}"
    else:
        kind = "artifact"
        label = path.name
    return E2ERunArtifactRecord(kind=kind, label=label, path=str(path))


def _dedupe_artifacts(
    artifacts: list[E2ERunArtifactRecord],
) -> list[E2ERunArtifactRecord]:
    deduped: list[E2ERunArtifactRecord] = []
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        key = (artifact.kind, artifact.path)
        if key in seen:
            continue
        deduped.append(artifact)
        seen.add(key)
    return deduped
