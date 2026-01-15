"""Code review checks for doctor."""

from ..types import Check
from ...config import Config


def check_code_review(config: Config) -> list[Check]:
    checks: list[Check] = []

    if config.review_enabled:
        if config.code_review_agent:
            if config.code_review_agent in config.agents:
                per_agent = [
                    (name, a.reviewer)
                    for name, a in config.agents.items()
                    if a.reviewer
                ]
                if per_agent:
                    invalid = [f"{n}→{r}" for n, r in per_agent if r not in config.agents]
                    if invalid:
                        checks.append(Check(
                            name="Code Review",
                            status="error",
                            detail=f"Invalid per-agent reviewers: {', '.join(invalid)}",
                        ))
                    else:
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
                    status="error",
                    detail=f"Default reviewer '{config.code_review_agent}' not in agents",
                ))
        else:
            checks.append(Check(
                name="Code Review",
                status="error",
                detail="Enabled but no default reviewer set",
            ))
    else:
        checks.append(Check(
            name="Code Review",
            status="info",
            detail="Disabled",
        ))

    return checks
