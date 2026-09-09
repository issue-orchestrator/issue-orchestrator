"""Validated-work retention parsing and minimal YAML serialization."""

from typing import TYPE_CHECKING

from .config_models import ValidatedWorkConfig

if TYPE_CHECKING:
    from .config import Config


def parse_validated_work_config(data: dict) -> ValidatedWorkConfig:
    return ValidatedWorkConfig(
        escrow_retention_days=data.get("escrow_retention_days", 30),
        drain_batch_size=data.get("drain_batch_size", 5),
        drain_interval_seconds=data.get("drain_interval_seconds", 60)
    )


def validated_work_section(config: "Config") -> dict:
    values = config.validated_work
    return {name: getattr(values, name) for name, default in
        (("escrow_retention_days", 30), ("drain_batch_size", 5), ("drain_interval_seconds", 60))
        if getattr(values, name) != default}
