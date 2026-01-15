"""E2E runner checks for doctor."""

import os
from pathlib import Path

from ..types import Check
from ...config import Config


def check_e2e_runner(config: Config) -> list[Check]:
    checks: list[Check] = []

    if config.e2e.enabled:
        repo_root = Path.cwd()
        e2e_checks = []

        if config.e2e.pytest_args:
            test_path = config.e2e.pytest_args[0]
            test_dir = repo_root / test_path
            if test_dir.exists():
                e2e_checks.append(f"tests: {test_path}")
            else:
                checks.append(Check(
                    name="E2E Runner",
                    status="warning",
                    detail=f"Test path '{test_path}' not found",
                ))

        quarantine_path = repo_root / config.e2e.quarantine_file
        if quarantine_path.exists():
            try:
                lines = [
                    l.strip()
                    for l in quarantine_path.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")
                ]
                e2e_checks.append(f"quarantine: {len(lines)} tests")
            except Exception:
                e2e_checks.append("quarantine: unreadable")
        else:
            e2e_checks.append("quarantine: none")

        db_dir = repo_root / ".issue-orchestrator"
        if db_dir.exists() and os.access(db_dir, os.W_OK):
            e2e_checks.append("db: writable")
        elif not db_dir.exists():
            e2e_checks.append("db: will create")
        else:
            checks.append(Check(
                name="E2E Runner",
                status="error",
                detail=".issue-orchestrator not writable",
            ))

        if not any(c.name == "E2E Runner" for c in checks):
            auto = (
                f"auto={config.e2e.auto_run_interval_minutes}m"
                if config.e2e.auto_run_interval_minutes > 0
                else "manual"
            )
            retry = "retry=on" if config.e2e.allow_retry_once else "retry=off"
            checks.append(Check(
                name="E2E Runner",
                status="ok",
                detail=f"Enabled ({auto}, {retry}, {', '.join(e2e_checks)})",
            ))
    else:
        checks.append(Check(
            name="E2E Runner",
            status="info",
            detail="Disabled",
        ))

    return checks
