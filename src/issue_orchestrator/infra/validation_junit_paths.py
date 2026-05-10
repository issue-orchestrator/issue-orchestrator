"""Helpers for validation JUnit report path configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_JUNIT_PATH_SECTIONS = ("validation",)


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
