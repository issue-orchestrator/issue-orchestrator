"""Standalone control center server.

This serves the control center UI and API without requiring an orchestrator
to be running. It can start/stop orchestrators for any registered repository.

Usage:
    python -m issue_orchestrator.entrypoints.control_center [--port 19080]

The control center will be available at http://127.0.0.1:19080/
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .control_api import control_app

logger = logging.getLogger(__name__)


def main() -> int:
    """Run the standalone control center server."""
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

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print(f"Starting Control Center on http://{args.host}:{args.port}/")
    print("Press Ctrl+C to stop")

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


if __name__ == "__main__":
    sys.exit(main())
