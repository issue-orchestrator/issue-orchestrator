"""Unknown fields validator."""

from typing import TYPE_CHECKING

from ..config_schema import (
    DynamicMap,
    LEAF,
    OPEN_MAP,
    ConfigShape,
    allowed_config_shape,
)
from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class UnknownFieldsValidator(ConfigValidator):
    """Validates that no unknown fields are present in config."""

    def validate(self, config: "Config") -> list[str]:
        """Validate unknown fields.

        Unknown fields are always errors. Config files are operator intent; a
        typo or indentation mistake must stop startup instead of silently
        changing orchestration scope.
        """
        return [
            f"Unknown config field: '{field_path}'"
            for field_path, _ in self.find_unknown_fields(config)
        ]

    def find_unknown_fields(self, config: "Config") -> list[tuple[str, str]]:
        """Find unknown fields in config.

        Returns list of (field_path, level) tuples where:
        - field_path is like "repo.root" or "agents.agent:web.some_field"
        - level is the nearest section that owns the path
        """
        if not isinstance(config.raw_data, dict):
            return []
        return self._find_unknown_fields(
            data=config.raw_data,
            shape=allowed_config_shape(),
            path="",
            level="top",
        )

    def _find_unknown_fields(
        self,
        data: object,
        shape: ConfigShape,
        path: str,
        level: str,
    ) -> list[tuple[str, str]]:
        if shape is OPEN_MAP or shape is LEAF or not isinstance(data, dict):
            return []

        if isinstance(shape, DynamicMap):
            unknown: list[tuple[str, str]] = []
            for key, value in data.items():
                child_path = f"{path}.{key}" if path else str(key)
                unknown.extend(
                    self._find_unknown_fields(
                        data=value,
                        shape=shape.value_schema,
                        path=child_path,
                        level=level,
                    )
                )
            return unknown

        if not isinstance(shape, dict):
            return []

        unknown = []
        for key, value in data.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in shape:
                unknown.append((child_path, level))
                continue
            child_level = child_path if not path else level
            unknown.extend(
                self._find_unknown_fields(
                    data=value,
                    shape=shape[key],
                    path=child_path,
                    level=child_level,
                )
            )
        return unknown
