"""Run-scoped runtime stdout/stderr capture for E2E test rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .e2e_reports import MAX_CAPTURED_OUTPUT_CHARS


@dataclass(frozen=True)
class RuntimeCapturedOutput:
    """Captured stdout/stderr persisted outside SQLite."""

    nodeid: str
    system_out: str | None
    system_err: str | None
    source_path: Path

    def to_payload(self) -> dict[str, str | None]:
        return {
            "nodeid": self.nodeid,
            "system_out": self.system_out,
            "system_err": self.system_err,
            "source_path": str(self.source_path),
        }


def runtime_output_dir(repo_root: Path, run_id: int) -> Path:
    """Return the run-scoped runtime output directory."""
    return (
        repo_root
        / ".issue-orchestrator"
        / "e2e-results"
        / f"run_{run_id}"
        / "runtime-output"
    )


def runtime_output_path(repo_root: Path, run_id: int, nodeid: str) -> Path:
    """Return the deterministic output file path for one nodeid."""
    digest = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()
    return runtime_output_dir(repo_root, run_id) / f"{digest}.json"


def write_runtime_captured_output(
    repo_root: Path,
    run_id: int,
    nodeid: str,
    *,
    system_out: str | None,
    system_err: str | None,
) -> RuntimeCapturedOutput | None:
    """Persist captured output for one runtime-observed test result."""
    cleaned_out = _clean_channel(system_out)
    cleaned_err = _clean_channel(system_err)
    if cleaned_out is None and cleaned_err is None:
        return None

    path = runtime_output_path(repo_root, run_id, nodeid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "nodeid": nodeid,
        "system_out": cleaned_out,
        "system_err": cleaned_err,
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(path)
    return RuntimeCapturedOutput(
        nodeid=nodeid,
        system_out=cleaned_out,
        system_err=cleaned_err,
        source_path=path,
    )


def write_pytest_report_captured_output(
    repo_root: Path,
    run_id: int,
    nodeid: str,
    report: object,
) -> RuntimeCapturedOutput | None:
    """Persist captured stdout/stderr from a pytest TestReport."""
    return write_runtime_captured_output(
        repo_root,
        run_id,
        nodeid,
        system_out=_pytest_report_system_out(report),
        system_err=_pytest_report_system_err(report),
    )


def read_runtime_captured_output(
    repo_root: Path,
    run_id: int,
    nodeid: str,
) -> RuntimeCapturedOutput | None:
    """Read runtime-captured output for one nodeid if it exists."""
    path = runtime_output_path(repo_root, run_id, nodeid)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("nodeid") != nodeid:
        return None
    system_out = _clean_channel(data.get("system_out"))
    system_err = _clean_channel(data.get("system_err"))
    if system_out is None and system_err is None:
        return None
    return RuntimeCapturedOutput(
        nodeid=nodeid,
        system_out=system_out,
        system_err=system_err,
        source_path=path,
    )


def _pytest_report_system_out(report: object) -> str | None:
    parts = [
        str(value).strip()
        for value in (
            getattr(report, "capstdout", None),
            getattr(report, "caplog", None),
        )
        if isinstance(value, str) and value.strip()
    ]
    return "\n".join(parts) if parts else None


def _pytest_report_system_err(report: object) -> str | None:
    value = getattr(report, "capstderr", None)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_channel(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_CAPTURED_OUTPUT_CHARS:
        return text[:MAX_CAPTURED_OUTPUT_CHARS]
    return text
