"""Event emission for subprocesses.

This module provides a simple way for subprocesses (like validation hooks)
to emit events to the orchestrator. Events are sent via the IPC socket
if available, otherwise just logged.

Usage:
    from issue_orchestrator.emit import emit_event

    emit_event("validation.started", {"sha": "abc123", "command": "make test"})
    # ... do work ...
    emit_event("validation.completed", {"sha": "abc123", "passed": True})

Environment:
    ORCHESTRATOR_IPC_SOCKET: Path to the orchestrator's IPC socket.
                             If not set or socket doesn't exist, events are logged.
"""

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def emit_event(name: str, data: dict[str, Any] | None = None) -> bool:
    """Emit an event to the orchestrator.

    If the orchestrator's IPC socket is available, sends the event there.
    Otherwise, logs the event for debugging.

    This is a fire-and-forget operation - it won't block or fail if
    the orchestrator isn't running.

    Args:
        name: Event name (e.g., "validation.started")
        data: Event data dictionary

    Returns:
        True if event was sent to socket, False if logged only
    """
    event = {
        "type": "event",
        "name": name,
        "data": data or {},
    }

    socket_path = os.environ.get("ORCHESTRATOR_IPC_SOCKET")

    if socket_path and Path(socket_path).exists():
        try:
            return _send_to_socket(socket_path, event)
        except Exception as e:
            logger.debug(f"Failed to send event to socket: {e}")
            # Fall through to logging

    # No socket or send failed - just log
    logger.info(f"[EVENT] {name}: {data}")
    return False


def _send_to_socket(socket_path: str, event: dict) -> bool:
    """Send an event to the IPC socket.

    Uses a synchronous socket connection with a short timeout.
    Fire-and-forget: sends the event and closes immediately.

    Args:
        socket_path: Path to the Unix socket
        event: Event dictionary to send

    Returns:
        True if sent successfully
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)  # 500ms timeout - don't block validation

    try:
        sock.connect(socket_path)
        message = json.dumps(event) + "\n"
        sock.sendall(message.encode("utf-8"))
        return True
    finally:
        sock.close()


def get_default_socket_path() -> Path:
    """Get the default IPC socket path.

    Returns:
        Path to the default socket location
    """
    return Path(f"/tmp/issue-orchestrator-{os.getuid()}.sock")
