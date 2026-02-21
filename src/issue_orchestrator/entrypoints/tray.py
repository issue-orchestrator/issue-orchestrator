"""System tray icon for issue-orchestrator Control Center.

Provides a menu bar icon (macOS) / system tray icon (Windows/Linux) that shows
engine status and offers quick access to the Control Center dashboard.

All dependencies (pystray, PIL) are imported lazily so this module degrades
gracefully when they are unavailable (e.g., headless servers, CI).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import urljoin, urlparse
from pathlib import Path
from typing import Any, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "assets"


def _process_exists(pid: int) -> bool:
    """Return whether a process exists for the given PID."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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

        running_engines = [(name, state) for name, state in engines if state == "running"]
        for name, state in running_engines:
            items.append(
                _pystray.MenuItem(
                    f"\u25cf {name}  ({state})",
                    None,
                    enabled=False,
                )
            )
        if running_engines:
            items.append(_pystray.Menu.SEPARATOR)
        else:
            items.append(_pystray.MenuItem("No running engines", None, enabled=False))
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
    """Start the system tray icon in detached mode.

    Returns the Icon instance so the caller can call ``icon.stop()`` on shutdown.
    """
    import pystray as _pystray

    image = icon_image or _load_icon()
    menu = _build_menu(dashboard_url, engine_status_fn)
    icon = _pystray.Icon("issue-orchestrator", image, "Issue Orchestrator", menu)
    icon.run_detached()
    return icon


def _engine_status_from_control_center(dashboard_url: str) -> list[tuple[str, str]]:
    """Fetch engine status snapshot from Control Center HTTP API."""
    repos_url = urljoin(dashboard_url.rstrip("/") + "/", "control/repos")
    req = urllib.request.Request(repos_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    repos = payload.get("repos")
    if not isinstance(repos, list):
        return []
    result: list[tuple[str, str]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        name = repo.get("name")
        if not isinstance(name, str):
            continue
        status = repo.get("status")
        state = status.get("state", "unknown") if isinstance(status, dict) else "unknown"
        running_count = status.get("running_count", 0) if isinstance(status, dict) else 0
        if isinstance(running_count, int) and running_count > 0:
            state = "running"
        if not isinstance(state, str):
            state = "unknown"
        result.append((name, state))
    return result


def run_tray_forever(dashboard_url: str, owner_pid: int | None = None) -> None:
    """Run tray icon on the current thread until process exits."""
    import pystray as _pystray

    stop_refresh = threading.Event()
    image = _load_icon()
    menu = _build_menu(
        dashboard_url=dashboard_url,
        engine_status_fn=lambda: _engine_status_from_control_center(dashboard_url),
    )
    icon = _pystray.Icon("issue-orchestrator", image, "Issue Orchestrator", menu)

    def _refresh_menu_loop() -> None:
        while not stop_refresh.wait(3.0):
            try:
                icon.update_menu()
            except Exception:
                logger.debug("Tray menu refresh failed", exc_info=True)

    threading.Thread(target=_refresh_menu_loop, daemon=True).start()
    if owner_pid:
        # Prevent orphaned tray icons if Control Center exits unexpectedly.
        def _owner_watchdog() -> None:
            while True:
                time.sleep(2.0)
                if not _process_exists(owner_pid):
                    os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_owner_watchdog, daemon=True).start()
    try:
        icon.run()
    finally:
        stop_refresh.set()


def _dashboard_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("dashboard URL must be absolute http(s) URL")
    return value


def main() -> int:
    """Run tray entrypoint for helper-process mode."""
    parser = argparse.ArgumentParser(description="Issue Orchestrator tray helper")
    parser.add_argument(
        "--dashboard-url",
        required=True,
        type=_dashboard_url,
        help="Control Center URL, e.g. http://127.0.0.1:19080/",
    )
    parser.add_argument(
        "--owner-pid",
        type=int,
        default=None,
        help="Owning Control Center PID; tray exits automatically if owner dies",
    )
    args = parser.parse_args()
    if args.owner_pid and not _process_exists(args.owner_pid):
        return 0
    run_tray_forever(args.dashboard_url, owner_pid=args.owner_pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
