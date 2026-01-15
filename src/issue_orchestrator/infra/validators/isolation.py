"""Isolation configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class IsolationValidator(ConfigValidator):
    """Validates isolation mode configuration.

    Checks:
    - isolation.mode is one of the valid modes
    """

    VALID_MODES = {"standard", "hardened"}

    def validate(self, config: "Config") -> list[str]:
        errors = []

        if config.isolation.mode not in self.VALID_MODES:
            errors.append(
                f"isolation.mode must be one of {self.VALID_MODES}, got: '{config.isolation.mode}'"
            )

        return errors
