"""Entrypoint for running the orchestrator as a subprocess.

This module is invoked by the supervisor to start an orchestrator instance:

    python -m issue_orchestrator.entrypoints.run_orchestrator \
        --repo-root /path/to/repo \
        --port 8080

It handles:
- Acquiring the repository lock
- Building the orchestrator
- Running startup
- Starting the web dashboard
- Releasing the lock on exit
"""

import argparse
import asyncio
import atexit
import logging
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the issue orchestrator for a repository"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="Repository root path",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port for web dashboard (default: 8080)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config file (optional, will search if not provided)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open browser (used when started via control center)",
    )
    return parser.parse_args()


async def run(
    repo_root: Path,
    port: int,
    config_path: Path | None,
    no_browser: bool = False,
) -> None:
    """Run the orchestrator with web dashboard.

    Args:
        repo_root: Repository root path
        port: HTTP port for web dashboard
        config_path: Optional path to config file
        no_browser: If True, don't auto-open browser
    """
    from ..entrypoints.bootstrap import build_orchestrator
    from ..entrypoints.web import run_with_web_dashboard
    from ..infra.config import Config
    from ..infra.repo_lock import acquire_lock, release_lock

    # Acquire the repository lock
    logger.info("Acquiring lock for %s", repo_root)
    lock_info = acquire_lock(repo_root, port)
    logger.info("Lock acquired: pid=%d, port=%s", lock_info.pid, lock_info.http_port)

    # Register cleanup on exit
    def cleanup():
        logger.info("Releasing lock for %s", repo_root)
        release_lock(repo_root)

    atexit.register(cleanup)

    # Load config
    if config_path:
        config = Config.load(config_path)
    else:
        config = Config.find_and_load(repo_root)

    # Override repo_root in config
    config.repo_root = repo_root

    # Build orchestrator
    logger.info("Building orchestrator...")
    orchestrator = build_orchestrator(config)

    # Import here to avoid circular import at module level
    from .web import trigger_server_shutdown

    # Set up asyncio-safe signal handlers (must be done inside running event loop)
    def handle_signal():
        logger.info("Received shutdown signal, requesting shutdown")
        orchestrator.request_shutdown()
        trigger_server_shutdown()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    # Run with web dashboard
    logger.info("Starting orchestrator on port %d", port)
    await run_with_web_dashboard(orchestrator, port, open_browser=not no_browser)


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Change to repo root
    import os

    os.chdir(args.repo_root)

    try:
        asyncio.run(run(args.repo_root, args.port, args.config, args.no_browser))
        return 0
    except Exception as e:
        logger.exception("Orchestrator failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
