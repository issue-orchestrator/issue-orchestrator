"""System tray icon for issue-orchestrator Control Center.

Provides a menu bar icon (macOS) / system tray icon (Windows/Linux) that shows
engine status and offers quick access to the Control Center dashboard.

All dependencies (pystray, PIL) are imported lazily so this module degrades
gracefully when they are unavailable (e.g., headless servers, CI).
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "assets"


def _load_icon() -> Image.Image:
    """Load tray icon from assets, falling back to a generated icon."""
    from PIL import Image as PILImage, ImageDraw

    png_path = _ASSETS_DIR / "tray-icon.png"
    if png_path.exists():
        return PILImage.open(png_path)

    # Fallback: generated blue circle (indigo-500, matches logo)
    size = 64
    image = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([4, 4, size - 4, size - 4], fill=(99, 102, 241))
    return image


def _build_menu(
    dashboard_url: str,
    engine_status_fn: Callable[[], list[tuple[str, str]]],
) -> Any:
    """Build dynamic menu — called each time the tray menu opens."""
    import pystray as _pystray

    def _menu_items() -> list[Any]:
        items: list[Any] = []
        try:
            engines = engine_status_fn()
        except Exception:
            engines = []

        for name, state in engines:
            bullet = "\u25cf" if state == "running" else "\u25cb"
            items.append(
                _pystray.MenuItem(f"{bullet} {name}  ({state})", None, enabled=False)
            )
        if engines:
            items.append(_pystray.Menu.SEPARATOR)

        items.append(
            _pystray.MenuItem(
                "Open Control Center",
                lambda: webbrowser.open(dashboard_url),
            )
        )
        return items

    return _pystray.Menu(_menu_items)


def start_tray(
    dashboard_url: str,
    engine_status_fn: Callable[[], list[tuple[str, str]]],
    icon_image: Image.Image | None = None,
) -> Any:
    """Start the system tray icon in a background thread.

    Returns the Icon instance so the caller can call ``icon.stop()`` on shutdown.
    """
    import pystray as _pystray

    image = icon_image or _load_icon()
    menu = _build_menu(dashboard_url, engine_status_fn)
    icon = _pystray.Icon("issue-orchestrator", image, "Issue Orchestrator", menu)

    def _run() -> None:
        try:
            icon.run()
        except Exception:
            logger.debug("Tray icon event loop failed", exc_info=True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return icon
