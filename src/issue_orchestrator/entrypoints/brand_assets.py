"""Package-owned brand assets served by the web entry points."""

from __future__ import annotations

from pathlib import Path

BRAND_ASSETS_DIR = Path(__file__).resolve().parent.parent / "static" / "brand"
LOGO_SVG_PATH = BRAND_ASSETS_DIR / "logo.svg"
TRAY_ICON_PNG_PATH = BRAND_ASSETS_DIR / "tray-icon.png"

LOGO_SVG_BYTES = LOGO_SVG_PATH.read_bytes()


def read_logo_svg() -> bytes:
    """Return the packaged logo SVG, failing loudly if packaging drift removes it."""
    return LOGO_SVG_BYTES
