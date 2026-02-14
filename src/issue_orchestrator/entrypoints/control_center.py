"""Unified dashboard server for issue-orchestrator.

This serves the unified dashboard UI and API. It manages orchestrators for
any registered repository and provides a single entry point for the user.

Usage:
    python -m issue_orchestrator.entrypoints.control_center [--port 19080]
    # Or via CLI: issue-orchestrator (no args)

The dashboard will be available at http://127.0.0.1:19080/
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from typing import Any

import uvicorn

from .control_api import control_app
from ..observation.instance_detector import write_dashboard_pid, clear_dashboard_pid

logger = logging.getLogger(__name__)

# Flag to signal reaper thread to stop
_reaper_stop = threading.Event()


def _zombie_reaper(interval: float = 1.0) -> None:
    """Background thread to reap zombie orchestrator child processes.

    Only reaps PIDs that were registered via control_api.track_child_pid().
    This avoids racing with subprocess.run() for unrelated children (e.g.,
    hook verification).

    This is needed because:
    - Control center starts orchestrators as child processes
    - When orchestrators exit, they become zombies until reaped
    - We can't use SIGCHLD=SIG_IGN because it breaks subprocess.run() on macOS
    - This thread periodically reaps tracked zombie PIDs without affecting
      subprocess exit codes for unrelated children
    """
    from .control_api import get_tracked_pids, untrack_child_pid

    while not _reaper_stop.is_set():
        pids_to_check = get_tracked_pids()

        for pid in pids_to_check:
            try:
                # Try to reap this specific PID (non-blocking)
                reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
                if reaped_pid != 0:
                    logger.debug("Reaped zombie orchestrator process %d", reaped_pid)
                    untrack_child_pid(reaped_pid)
            except ChildProcessError:
                # Process doesn't exist or isn't our child - remove from tracking
                untrack_child_pid(pid)
            except Exception as e:
                logger.debug("Error reaping PID %d: %s", pid, e)

        _reaper_stop.wait(interval)


def _open_browser(url: str, delay: float = 1.0) -> None:
    """Open browser after a short delay to let server start."""
    time.sleep(delay)
    webbrowser.open(url)


def _start_tray_icon(url: str) -> Any | None:
    """Start the system tray icon, returning the icon or None on failure."""
    try:
        from .tray import start_tray
        from .control_api import _build_repos_status

        def _get_engine_status() -> list[tuple[str, str]]:
            try:
                return [
                    (r["name"], r.get("status", {}).get("state", "unknown"))
                    for r in _build_repos_status()
                ]
            except Exception:
                return []

        icon = start_tray(dashboard_url=url, engine_status_fn=_get_engine_status)
        logger.debug("System tray icon started")
        return icon
    except Exception:
        logger.debug("System tray icon unavailable (headless or missing deps)")
        return None


def main() -> int:
    """Run the standalone control center server."""
    # NOTE: Cannot use signal.signal(signal.SIGCHLD, signal.SIG_IGN) to auto-reap
    # zombie child processes because on macOS it breaks subprocess.run() by causing
    # waitpid() to return exit code 0 instead of the actual exit code. Instead, we
    # use a background thread (_zombie_reaper) that periodically calls waitpid()
    # with WNOHANG to reap zombies without affecting subprocess exit codes.

    parser = argparse.ArgumentParser(
        description="Issue Orchestrator Control Center - manage orchestrators",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=19080,
        help="Port to listen on (default: 19080)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open browser automatically",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Don't show system tray icon",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Suppress httpx INFO logging (polls orchestrator status every 3s)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Clean up stale repos from registry (e.g., deleted directories, old test paths)
    from ..infra.repo_registry import cleanup_stale_repos
    stale_count = cleanup_stale_repos()
    if stale_count > 0:
        logger.info("Cleaned up %d stale repo(s) from registry", stale_count)

    url = f"http://{args.host}:{args.port}/"
    print(f"Starting Issue Orchestrator Dashboard on {url}")
    print("Press Ctrl+C to stop")

    # Write PID file for detection by CLI
    write_dashboard_pid(args.port)

    # Open browser in background thread after server starts
    if not args.no_browser:
        thread = threading.Thread(target=_open_browser, args=(url,), daemon=True)
        thread.start()

    # Start zombie reaper thread to clean up exited orchestrator processes
    # Only on Unix-like systems where waitpid is available
    if hasattr(os, "waitpid") and os.name != "nt":
        _reaper_stop.clear()
        reaper_thread = threading.Thread(target=_zombie_reaper, daemon=True)
        reaper_thread.start()

    # Start system tray icon (menu bar on macOS)
    tray_icon = _start_tray_icon(url) if not args.no_tray else None

    try:
        uvicorn.run(
            control_app,
            host=args.host,
            port=args.port,
            log_level="info" if not args.debug else "debug",
        )
        return 0
    except KeyboardInterrupt:
        print("\nShutting down...")
        return 0
    except Exception as e:
        logger.exception("Control center failed: %s", e)
        return 1
    finally:
        # Stop the system tray icon
        if tray_icon is not None:
            tray_icon.stop()
        # Stop the reaper thread
        _reaper_stop.set()
        # Clear PID file
        clear_dashboard_pid()


if __name__ == "__main__":
    sys.exit(main())
