"""Typed helpers for generic E2E result and artifact reporting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree


CaseOutcome = Literal["passed", "failed", "error", "skipped"]


@dataclass(frozen=True)
class JUnitCaseResult:
    """One structured result case parsed from a JUnit XML report."""

    case_id: str
    display_name: str
    suite_name: str | None
    outcome: CaseOutcome
    duration_seconds: float | None
    failure_details: str | None = None

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


def parse_junit_report(path: Path) -> list[JUnitCaseResult]:
    """Parse JUnit XML into strongly typed case results.

    Raises:
        ValueError: when the file is missing or structurally malformed.
    """
    if not path.exists():
        raise ValueError(f"JUnit XML does not exist: {path}")

    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise ValueError(f"Malformed JUnit XML: {path}") from exc

    cases = root.findall(".//testcase")
    if not cases:
        raise ValueError(f"JUnit XML did not contain any <testcase> entries: {path}")

    return [_parse_testcase(case) for case in cases]


def _parse_testcase(testcase: ElementTree.Element) -> JUnitCaseResult:
    display_name = (testcase.attrib.get("name") or "").strip()
    if not display_name:
        raise ValueError("JUnit testcase is missing a non-empty name attribute")

    suite_name = _optional_text(testcase.attrib.get("classname"))
    case_id = f"{suite_name}::{display_name}" if suite_name else display_name
    duration_seconds = _optional_float(testcase.attrib.get("time"))

    failure = testcase.find("failure")
    if failure is not None:
        return JUnitCaseResult(
            case_id=case_id,
            display_name=display_name,
            suite_name=suite_name,
            outcome="failed",
            duration_seconds=duration_seconds,
            failure_details=_detail_text(failure),
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
        )

    return JUnitCaseResult(
        case_id=case_id,
        display_name=display_name,
        suite_name=suite_name,
        outcome="passed",
        duration_seconds=duration_seconds,
    )


def _detail_text(element: ElementTree.Element) -> str | None:
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
