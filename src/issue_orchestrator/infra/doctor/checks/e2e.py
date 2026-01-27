"""E2E runner checks for doctor.

Path validation (quarantine_file, pytest_args test directory) is now
schema-driven via doctor_check annotations in settings_schema.py.
This module handles:
- Status summary (Enabled/Disabled with detail)
- DB directory writability check (not a schema field)
- Quarantine file line count (runtime inspection, not schema-validatable)
"""

import os
from pathlib import Path

from ..types import Check
from ...config import Config
from .schema import format_summary


def check_e2e_runner(config: Config) -> list[Check]:
    checks: list[Check] = []

    if config.e2e.enabled:
        repo_root = Path.cwd()
        e2e_parts: list[str] = []

        # Schema-driven summary parts (auto, retry, tests path)
        summary = format_summary("e2e", config)
        if summary:
            e2e_parts.append(summary)

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
