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
ASKING_PROVISIONING_CODER_LABEL = "agent:exam-coder-asks-provisioning"
"""A coder that asks for account provisioning only a human can do (Cases F/G)."""
SPLIT_UNTIL_RESOLVED_CODER_LABEL = "agent:exam-coder-asks-split"
"""A coder that asks the split question until the tech lead resolves it (Cases F/G)."""
SPEC_QUESTION_BESIDE_PR_CODER_LABEL = "agent:exam-coder-asks-spec-beside-pr"
"""A coder that publishes and asks, beside its PR, a question its spec answers (Cases F/G)."""
GIVES_UP_CODER_LABEL = "agent:exam-coder-gives-up"
"""A coder that ends without a completion until the tech lead resolves its
block (Cases F/G)."""
RULED_CODER_LABEL = "agent:exam-coder-ruled"
"""Case I's coder on the ruled item: it codes as ``CODER_LABEL`` does (so its
rework extends what the ruling retires) and captures every prompt it gets."""
ANSWERABLE_CODER_LABEL = "agent:exam-coder-asks-answerable"
"""Case I's coder that asks, before any work, a question its issue's spec
answers, until the tech lead resolves it (porchpin#327)."""

#: The question Case I's answerable coder asks (porchpin#327's shape).
ANSWERABLE_QUESTION = (
    "Before I start: should the buyer contact index be its own table with a deletion"
    " fence, or should I widen the seller index to carry buyers too?"
)

#: The question Case D's asking coder puts to the operator (porchpin#262's).
SPLIT_QUESTION = (
    "This issue is more than one session. Its first slice is done and gate-green on"
    " this branch; the rest of the acceptance list is not started. Should I split it:"
    " land this slice as a PR under 'Refs' and move the rest into its own issue?"
)


#: The question Cases F/G's beside-PR coder asks: the issue's spec answers it.
BESIDE_PR_QUESTION = (
    "A1 still needs the maintainer: should the batch hold its Delivery-owner"
    " provenance, or is it ruled unholdable?"
)

#: porchpin#179's question: human-only work, never resolvable.
PROVISIONING_QUESTION = (
    "The cloud-test deployment needs a Cloudflare account and an API token with"
    " Workers permissions, and the GitHub environment needs the deploy token as a"
    " secret. I cannot provision those: please create the account and add the token."
)


def shim_command(
    role: str,
    *,
    exchange_fault: str = "none",
    hold_until: Path | None = None,
    asks: str | None = None,
    pr_labels: tuple[str, ...] = (),
    changes_once: Path | None = None,
    gives_up: bool = False,
    until_resolved: bool = False,
    capture_prompts: Path | None = None,
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
            *(("--gives-up",) if gives_up else ()),
            *(("--until-resolved",) if until_resolved else ()),
            *(("--capture-prompts", str(capture_prompts)) if capture_prompts is not None else ()),
        )
    )
