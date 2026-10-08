"""CI-failure triage config parsing and minimal YAML serialization (#8692)."""

from dataclasses import fields
from typing import TYPE_CHECKING

from .config_models import CiFailureTriageConfig

if TYPE_CHECKING:
    from .config import Config


def _signature_list(data: dict, key: str) -> list[str] | None:
    raw = data.get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        raise ValueError(f"ci_failure_triage.{key} must be a list of regular expressions")
    return [str(item).strip() for item in raw if str(item).strip()]


def parse_ci_failure_triage_config(data: dict) -> CiFailureTriageConfig:
    defaults = CiFailureTriageConfig()
    transient = _signature_list(data, "transient_signatures")
    genuine = _signature_list(data, "genuine_signatures")
    return CiFailureTriageConfig(
        enabled=data.get("enabled", defaults.enabled),
        transient_signatures=defaults.transient_signatures if transient is None else transient,
        genuine_signatures=defaults.genuine_signatures if genuine is None else genuine,
        log_tail_bytes=data.get("log_tail_bytes", defaults.log_tail_bytes),
    )


def ci_failure_triage_section(config: "Config") -> dict:
    """Only the values that differ from the defaults."""
    values, defaults = config.ci_failure_triage, CiFailureTriageConfig()
    return {
        item.name: getattr(values, item.name)
        for item in fields(CiFailureTriageConfig)
        if getattr(values, item.name) != getattr(defaults, item.name)
    }
