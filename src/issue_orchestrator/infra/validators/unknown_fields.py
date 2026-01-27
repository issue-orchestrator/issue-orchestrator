"""Unknown fields validator."""

import logging
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


class UnknownFieldsValidator(ConfigValidator):
    """Validates that no unknown fields are present in config.

    In strict mode, unknown fields are errors.
    In non-strict mode, unknown fields are warnings.
    """

    def validate(self, config: "Config") -> list[str]:
        """Validate unknown fields based on config_strict setting."""
        return self.validate_with_strictness(config, config.config_strict)

    def validate_with_strictness(self, config: "Config", strict: bool) -> list[str]:
        """Validate with explicit strictness setting.

        Args:
            config: The configuration to validate
            strict: If True, unknown fields are errors. If False, warnings.

        Returns:
            List of error messages (only in strict mode)
        """
        from ..config import ALLOWED_TOP_LEVEL_FIELDS, ALLOWED_AGENT_FIELDS

        errors = []
        unknown_fields = self._find_unknown_fields(config, ALLOWED_TOP_LEVEL_FIELDS, ALLOWED_AGENT_FIELDS)

        for field_path, _ in unknown_fields:
            msg = f"Unknown config field: '{field_path}'"
            if strict:
                errors.append(msg)
            else:
                logger.warning(msg)

        return errors

    def _find_unknown_fields(
        self,
        config: "Config",
        allowed_top_level: AbstractSet[str],
        allowed_agent: AbstractSet[str],
    ) -> list[tuple[str, str]]:
        """Find unknown fields in config.

        Returns list of (field_path, level) tuples where:
        - field_path is like "repo.root" or "agents.agent:web.some_field"
        - level is "top" or "agent"
        """
        unknown = []

        # Check top-level fields
        for key in config.raw_data.keys():
            if key not in allowed_top_level:
                unknown.append((key, "top"))

        # Check per-agent fields
        for agent_name, agent_data in config.raw_agents.items():
            if isinstance(agent_data, dict):
                for key in agent_data.keys():
                    if key not in allowed_agent:
                        unknown.append((f"agents.{agent_name}.{key}", "agent"))

        return unknown
