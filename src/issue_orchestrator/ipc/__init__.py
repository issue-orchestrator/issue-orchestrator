"""IPC (Inter-Process Communication) module for decoupled UI processes.

.. deprecated::
    This module is deprecated in favor of HTTP + SSE (Server-Sent Events).
    Use the web module's SSE endpoints instead of Unix socket IPC.
    See LifecycleSSEPlugin for the preferred event streaming approach.

    Rationale: HTTP + SSE provides the same functionality with:
    - Standard HTTP tooling (curl, fetch, browsers)
    - No custom protocol/parsing
    - Better debugging and monitoring
    - Works across network boundaries

This module provides a socket-based event server that allows UI processes
(web, CLI, desktop) to run separately from the orchestrator and receive
real-time event notifications.

Architecture:
    Orchestrator Process          UI Process(es)
    ┌─────────────────┐          ┌─────────────────┐
    │ EventServer     │◄────────▶│ EventClient     │
    │ (Unix socket)   │          │                 │
    └─────────────────┘          └─────────────────┘

Usage:
    # In orchestrator
    server = EventServer("/tmp/orchestrator.sock")
    await server.start()
    await server.broadcast({"type": "session_started", ...})

    # In UI process
    client = EventClient("/tmp/orchestrator.sock")
    async for event in client.events():
        handle_event(event)
"""

from .server import EventServer
from .client import EventClient

__all__ = ["EventServer", "EventClient"]
