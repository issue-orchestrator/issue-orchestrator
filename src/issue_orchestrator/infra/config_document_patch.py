"""Patch-preserving YAML config persistence."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import yaml


class ConfigDocumentPatchTarget(Protocol):
    """Config-like object that identifies its source YAML path."""

    config_path: Path | None


def save_config_document_patch(
    config: ConfigDocumentPatchTarget,
    patch: Callable[[dict[str, Any]], Any],
    path: Path | None = None,
) -> Path:
    """Persist config by patching the existing on-disk YAML document.

    This reads the current YAML file, lets ``patch`` mutate the parsed mapping
    in place, and writes it back. Only the keys ``patch`` touches change; every
    unrelated section, key, and its order is preserved, and the file's leading
    comment/header block is re-emitted. The document is parsed without
    ``${VAR}`` expansion so referenced secrets are never materialized.
    """
    save_path = path or config.config_path
    if save_path is None:
        raise ValueError("No path specified and config_path is not set")

    document, header = _read_yaml_document_with_header(save_path)
    patch(document)

    with open(save_path, "w", encoding="utf-8") as f:
        if header:
            f.write(header)
        yaml.dump(
            document, f, default_flow_style=False, sort_keys=False, allow_unicode=True
        )

    return save_path


def _read_yaml_document_with_header(path: Path) -> tuple[dict[str, Any], str]:
    """Read a YAML file into a mapping plus its leading comment block."""
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ValueError(f"Config document at {path} is not a mapping")
    header_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            header_lines.append(line)
        else:
            break
    return document, "".join(header_lines)
