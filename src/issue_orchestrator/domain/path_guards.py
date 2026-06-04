"""Shared path invariants for typed domain assets."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock


def require_absolute_path(value: object, field_name: str) -> None:
    """Require a real absolute ``Path`` value.

    Tests sometimes pass spec'd mocks that satisfy broad attribute checks. These
    guards reject them at domain boundaries so typed asset contracts stay real.
    """
    if isinstance(value, Mock) or not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a pathlib.Path")
    if not value.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {value}")


def require_path_under(path: Path, root: Path, field_name: str) -> None:
    require_absolute_path(path, field_name)
    require_absolute_path(root, "root")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field_name} must live under {root}: {path}") from exc
