"""The exam's scripted agents: labels and the shim command (no e2e imports).

Kept free of the live-harness imports so unit tests can pin the command the
engine will run without importing the e2e conftest.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

SHIM = Path(__file__).resolve().parent / "shims" / "exam_agent.py"

CODER_LABEL = "agent:exam-coder"
REVIEWER_LABEL = "agent:exam-reviewer"
TECH_LEAD_LABEL = "agent:exam-tech-lead"
HELD_CODER_LABEL = "agent:exam-coder-held"
"""A coder that holds mid-flight until released (the upgrade case)."""


def shim_command(
    role: str, *, exchange_fault: str = "none", hold_until: Path | None = None
) -> str:
    """Agent command running the shim; no ``{}`` placeholders on purpose.

    The engine renders agent commands with ``str.format``; any brace here
    would crash the launch instead of planting the fault.
    """
    return " ".join(
        shlex.quote(part)
        for part in (
            sys.executable,
            "-u",
            str(SHIM),
            "--role",
            role,
            "--exchange-fault",
            exchange_fault,
            *(("--hold-until", str(hold_until)) if hold_until is not None else ()),
        )
    )
