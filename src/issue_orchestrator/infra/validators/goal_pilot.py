"""Goal Pilot configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class GoalPilotValidator(ConfigValidator):
    """Validates Goal Pilot configuration."""

    def validate(self, config: "Config") -> list[str]:
        errors: list[str] = []
        goal_pilot = config.goal_pilot
        if goal_pilot.enabled:
            if not goal_pilot.agent:
                errors.append(
                    "goal_pilot.enabled is true but no agent configured. "
                    "Add 'goal_pilot: agent: agent:goal-pilot' to config."
                )
            elif goal_pilot.agent not in config.agents:
                errors.append(
                    f"goal_pilot.agent '{goal_pilot.agent}' not found in agents. "
                    f"Available: {list(config.agents.keys())}"
                )
        elif goal_pilot.agent and goal_pilot.agent not in config.agents:
            errors.append(
                f"goal_pilot.agent '{goal_pilot.agent}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )
        return errors
