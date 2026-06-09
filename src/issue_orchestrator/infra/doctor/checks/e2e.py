"""E2E runner checks for doctor.

This module handles:
- Status summary (Enabled/Disabled with detail)
- Runner-target validation for pytest and generic command modes
- DB directory writability check (not a schema field)
- Quarantine file line count (runtime inspection, not schema-validatable)
"""

import os
from pathlib import Path

from ..types import Check
from ...config import Config


def _e2e_summary_parts(config: Config, repo_root: Path) -> tuple[list[str], list[Check]]:
    parts: list[str] = []
    checks: list[Check] = []
    try:
        spec = config.e2e.execution_spec()
    except ValueError as exc:
        checks.append(Check(
            name="E2E Runner",
            status="error",
            detail=str(exc),
        ))
        return parts, checks

    parts.append(f"runner={spec.runner_kind}")
    parts.append(f"target={spec.display_target}")
    if spec.allow_retry_once and spec.runner_kind == "pytest":
        parts.append("retry=on")
    elif spec.runner_kind == "pytest":
        parts.append("retry=off")
    else:
        parts.append("retry=n/a")

    if spec.runner_kind == "pytest" and spec.pytest_args:
        test_path = repo_root / spec.pytest_args[0]
        if not test_path.exists():
            checks.append(Check(
                name="E2E Runner",
                status="warning",
                detail=f"Pytest target does not exist: {spec.pytest_args[0]}",
            ))
    elif spec.runner_kind == "command" and spec.command:
        command_path = Path(spec.command[0])
        if ("/" in spec.command[0] or spec.command[0].startswith(".")) and not (repo_root / command_path).exists():
            checks.append(Check(
                name="E2E Runner",
                status="warning",
                detail=f"Command path does not exist: {spec.command[0]}",
            ))
        if not spec.junit_xml_paths:
            parts.append("results=log-only")
        else:
            parts.append(f"junit={len(spec.junit_xml_paths)}")

    return parts, checks


def check_e2e_runner(config: Config) -> list[Check]:
    checks: list[Check] = []

    if config.e2e.enabled:
        repo_root = config.repo_root
        e2e_parts: list[str] = []
        summary_parts, validation_checks = _e2e_summary_parts(config, repo_root)
        e2e_parts.extend(summary_parts)
        checks.extend(validation_checks)

        # Quarantine file line count — runtime inspection beyond path existence
        quarantine_path = repo_root / config.e2e.quarantine_file
        if quarantine_path.exists():
            try:
                lines = [
                    l.strip()
                    for l in quarantine_path.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")
                ]
                e2e_parts.append(f"quarantine: {len(lines)} tests")
            except Exception:
                e2e_parts.append("quarantine: unreadable")
        else:
            e2e_parts.append("quarantine: none")

        # DB directory writability — runtime check, not a schema field
        db_dir = repo_root / ".issue-orchestrator"
        if db_dir.exists() and os.access(db_dir, os.W_OK):
            e2e_parts.append("db: writable")
        elif not db_dir.exists():
            e2e_parts.append("db: will create")
        else:
            checks.append(Check(
                name="E2E Runner",
                status="error",
                detail=".issue-orchestrator not writable",
            ))

        if not any(c.name == "E2E Runner" and c.status == "error" for c in checks):
            checks.append(Check(
                name="E2E Runner",
                status="ok",
                detail=f"Enabled ({', '.join(e2e_parts)})",
            ))
    else:
        checks.append(Check(
            name="E2E Runner",
            status="info",
            detail="Disabled",
        ))

    return checks
