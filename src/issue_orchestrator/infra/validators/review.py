"""Review workflow configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class ReviewWorkflowValidator(ConfigValidator):
    """Validates review workflow configuration.

    Checks:
    - If reviews enabled, default reviewer must be set
    - Default reviewer must exist in agents
    - Triage review agent must exist in agents (if set)
    """

    def validate(self, config: "Config") -> list[str]:
        errors = []

        # Validate review workflow
        if config.review_enabled:
            if not config.code_review_agent:
                errors.append(
                    "review.enabled is true but no default reviewer set. "
                    "Add 'review: default: agent:reviewer' to config."
                )
            elif config.code_review_agent not in config.agents:
                errors.append(
                    f"review.default '{config.code_review_agent}' not found in agents. "
                    f"Available: {list(config.agents.keys())}"
                )

        # Validate triage review agent
        if config.triage_review_agent and config.triage_review_agent not in config.agents:
            errors.append(
                f"triage_review_agent '{config.triage_review_agent}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

        return errors
