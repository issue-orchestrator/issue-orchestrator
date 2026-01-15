"""Base class for config validators."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


class ConfigValidator(ABC):
    """Base class for configuration validators.

    Each validator checks one aspect of the configuration and
    returns a list of error messages (empty if valid).
    """

    @abstractmethod
    def validate(self, config: "Config") -> list[str]:
        """Validate the configuration.

        Args:
            config: The configuration to validate

        Returns:
            List of error messages (empty if valid)
        """
        ...
