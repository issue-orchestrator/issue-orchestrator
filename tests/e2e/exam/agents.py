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
ASKING_CODER_LABEL = "agent:exam-coder-asks"
"""A coder that ends by asking the operator a question, with no PR (Case D)."""
ASKING_BESIDE_PR_CODER_LABEL = "agent:exam-coder-asks-beside-pr"
"""A coder that publishes its work and asks a person to decide before it
merges (``--pr-labels needs-human``, Case E)."""

#: The question Case D's asking coder puts to the operator (porchpin#262's).
SPLIT_QUESTION = (
    "This issue is more than one session. Its first slice is done and gate-green on"
    " this branch; the rest of the acceptance list is not started. Should I split it:"
    " land this slice as a PR under 'Refs' and move the rest into its own issue?"
)


def shim_command(
    role: str,
    *,
    exchange_fault: str = "none",
    hold_until: Path | None = None,
    asks: str | None = None,
    pr_labels: tuple[str, ...] = (),
    changes_once: Path | None = None,
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
            *(("--asks", asks) if asks is not None else ()),
            *(part for label in pr_labels for part in ("--pr-label", label)),
            *(("--changes-once", str(changes_once)) if changes_once is not None else ()),
        )
    )
