from __future__ import annotations

import tomllib
from pathlib import Path


def test_tray_dependencies_are_declared() -> None:
    """Tray support must remain in declared project dependencies."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])

    assert any(dep.startswith("pystray") for dep in deps), "Missing pystray dependency"
    assert any(dep.startswith("Pillow") for dep in deps), "Missing Pillow dependency"
