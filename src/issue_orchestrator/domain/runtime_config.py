"""Typed runtime configuration identity passed into agent subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeConfigReference:
    """The selected orchestrator config file for a managed runtime action."""

    config_path: Path
    config_name: str

    def __post_init__(self) -> None:
        if not self.config_path.is_absolute():
            raise ValueError("config_path must be absolute")
        if not self.config_path.is_file():
            raise ValueError(
                f"config_path must point to an existing file: {self.config_path}"
            )
        if type(self.config_name) is not str or not self.config_name.strip():
            raise ValueError("config_name must be a non-empty string")

    @classmethod
    def from_path(cls, config_path: Path) -> "RuntimeConfigReference":
        resolved = config_path.expanduser().resolve()
        return cls(config_path=resolved, config_name=resolved.name)

    def to_env(self) -> dict[str, str]:
        return {
            "ISSUE_ORCHESTRATOR_CONFIG_NAME": self.config_name,
            "ISSUE_ORCHESTRATOR_CONFIG_PATH": str(self.config_path),
            "ORCHESTRATOR_CONFIG_NAME": self.config_name,
            "ORCHESTRATOR_CONFIG_PATH": str(self.config_path),
        }
