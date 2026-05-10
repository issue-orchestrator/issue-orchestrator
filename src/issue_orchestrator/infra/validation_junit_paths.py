"""Helpers for validation JUnit report path configuration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .e2e_reports import junit_report_modified_after

if TYPE_CHECKING:
    from ..ports.session_output import ValidationRecord

_JUNIT_PATH_SECTIONS = ("validation",)
logger = logging.getLogger(__name__)


def configured_validation_junit_xml_paths(config: Any) -> tuple[str, ...]:
    """Return validation report paths from validation config."""
    if config is None:
        return ()
    sections: dict[str, dict[str, object]] = {}
    for section_name in _JUNIT_PATH_SECTIONS:
        section = getattr(config, section_name, None)
        if section is None:
            continue
        sections[section_name] = {
            "junit_xml_paths": getattr(section, "junit_xml_paths", ()),
        }
    return configured_validation_junit_xml_paths_from_mapping(sections)


def configured_validation_junit_xml_paths_from_mapping(
    config: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return validation report paths from a parsed YAML config mapping."""
    paths: list[str] = []
    for section_name in _JUNIT_PATH_SECTIONS:
        section = config.get(section_name, {}) or {}
        if not isinstance(section, Mapping):
            continue
        paths.extend(_normalize_paths(section.get("junit_xml_paths", ())))
    return _dedupe_paths(paths)


def validation_record_started_epoch(record: ValidationRecord | None) -> float | None:
    """Return the validation record start time as an epoch timestamp."""
    if record is None:
        return None
    return validation_started_epoch(record.started_at)


def validation_record_junit_modified_after(
    record: ValidationRecord | None,
) -> float | None:
    """Return the JUnit freshness cutoff for a validation record."""
    return junit_report_modified_after(validation_record_started_epoch(record))


def validation_started_epoch(started_at: str | None) -> float | None:
    """Parse a validation ``started_at`` string into an epoch timestamp."""
    if not started_at:
        return None
    try:
        timestamp = (
            f"{started_at[:-1]}+00:00" if started_at.endswith("Z") else started_at
        )
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        logger.debug("Invalid validation started_at timestamp: %s", started_at)
        return None


def _normalize_paths(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(path) for path in value if path)
    return ()


def _dedupe_paths(paths: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(paths))
