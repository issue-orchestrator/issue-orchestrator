"""Template helpers shared by web route modules."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def get_templates() -> Environment:
    """Get Jinja2 template environment."""
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


__all__ = ["TEMPLATE_DIR", "get_templates"]
