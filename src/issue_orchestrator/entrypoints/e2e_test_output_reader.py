"""Load E2E per-test stdout/stderr payloads from run artifacts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

from ..contracts.ui_openapi_models import E2ETestOutputPayload

logger = logging.getLogger(__name__)


def load_e2e_test_output(
    *,
    repo_root: Path,
    run_id: int,
    nodeid: str,
    junit_paths: list[Path],
) -> E2ETestOutputPayload | None:
    """Load captured output from live runtime capture or JUnit XML."""
    runtime_payload = _captured_output_from_runtime(repo_root, nodeid, run_id=run_id)
    if runtime_payload is not None:
        return runtime_payload
    return _captured_output_from_junit(junit_paths, nodeid, run_id=run_id)


def _captured_output_from_runtime(
    repo_root: Path,
    nodeid: str,
    *,
    run_id: int,
) -> E2ETestOutputPayload | None:
    from ..infra.e2e_runtime_output import read_runtime_captured_output

    captured = read_runtime_captured_output(repo_root, run_id, nodeid)
    if captured is None:
        return None
    return E2ETestOutputPayload.model_validate(captured.to_payload())


def _captured_output_from_junit(
    junit_paths: list[Path],
    nodeid: str,
    *,
    run_id: int,
) -> E2ETestOutputPayload | None:
    for path, raw_case, norm_case in _iter_junit_case_rows(junit_paths, run_id=run_id):
        if nodeid not in (
            raw_case.case_id,
            norm_case.case_id,
            _legacy_pytest_junit_case_id(raw_case),
        ):
            continue
        if raw_case.system_out is None and raw_case.system_err is None:
            continue
        return E2ETestOutputPayload.model_validate(
            {
                "nodeid": nodeid,
                "system_out": raw_case.system_out,
                "system_err": raw_case.system_err,
                "source_path": str(path),
            }
        )
    return None


def _iter_junit_case_rows(
    junit_paths: list[Path],
    *,
    run_id: int,
) -> Iterator[tuple[Path, Any, Any]]:
    """Yield source path, raw case, and pytest-normalized case from run XMLs."""
    from ..infra.e2e_reports import parse_junit_report_cached

    for path in junit_paths:
        if not path.exists():
            logger.warning("JUnit XML for run %s missing on disk: %s", run_id, path)
            continue
        try:
            raw_cases, normalized_cases = parse_junit_report_cached(path)
        except ValueError:
            logger.warning("Skipping malformed JUnit XML for run %s: %s", run_id, path)
            continue
        for raw_case, norm_case in zip(raw_cases, normalized_cases):
            yield path, raw_case, norm_case


def _legacy_pytest_junit_case_id(raw_case: Any) -> str | None:
    """Return the pre-fix class-normalized nodeid for old DB rows."""
    suite_name = raw_case.suite_name
    if not suite_name or "/" in suite_name:
        return None
    parts = [part for part in str(suite_name).split(".") if part]
    if not parts or not any(part.startswith("test_") for part in parts):
        return None
    return f"{'/'.join(parts)}.py::{raw_case.display_name}"
