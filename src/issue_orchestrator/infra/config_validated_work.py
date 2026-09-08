"""Validated-work retention parsing and minimal YAML serialization."""

from typing import TYPE_CHECKING

from .config_models import ValidatedWorkConfig

if TYPE_CHECKING:
    from .config import Config


def parse_validated_work_config(data: dict) -> ValidatedWorkConfig:
    return ValidatedWorkConfig(
        escrow_retention_days=data.get("escrow_retention_days", 30)
    )


def validated_work_section(config: "Config") -> dict:
    days = config.validated_work.escrow_retention_days
    return {} if days == 30 else {"escrow_retention_days": days}
