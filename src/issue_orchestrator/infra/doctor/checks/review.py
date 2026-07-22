"""Code review checks for doctor.

Basic agent-reference validation (default_reviewer, tech_lead_agent must exist
in config.agents) is now schema-driven via doctor_check annotations.
This module handles:
- Status summary (Enabled/Disabled with per-agent reviewer detail)
- Per-agent reviewer cross-validation (each agent's .reviewer must be valid)
  This is inherently cross-field and stays as code.
"""

from ..types import Check
from ...config import Config


def check_code_review(config: Config) -> list[Check]:
    checks: list[Check] = []

    if config.review_enabled:
        if not config.code_review_agent:
            checks.append(Check(
                name="Code Review",
                status="error",
                detail="Enabled but no default reviewer set",
            ))
            return checks

        # Per-agent reviewer cross-validation — can't be expressed as a
        # single-field schema annotation because it iterates config.agents
        per_agent = [
            (name, a.reviewer)
            for name, a in config.agents.items()
            if a.reviewer
        ]
        if per_agent:
            invalid = [f"{n}\u2192{r}" for n, r in per_agent if r not in config.agents]
            if invalid:
                checks.append(Check(
                    name="Code Review",
                    status="error",
                    detail=f"Invalid per-agent reviewers: {', '.join(invalid)}",
                ))
                return checks
            checks.append(Check(
                name="Code Review",
                status="ok",
                detail=f"Enabled, default: {config.code_review_agent}, {len(per_agent)} per-agent",
            ))
        else:
            checks.append(Check(
                name="Code Review",
                status="ok",
                detail=f"Enabled, default: {config.code_review_agent}",
            ))
    else:
        checks.append(Check(
            name="Code Review",
            status="info",
            detail="Disabled",
        ))

    return checks
