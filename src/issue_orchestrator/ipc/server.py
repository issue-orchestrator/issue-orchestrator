"""Unix socket event server for broadcasting lifecycle events.

The EventServer runs in the orchestrator process and accepts connections
from any number of UI processes. When events occur, they are broadcast
to all connected clients.

Protocol:
    - Connection: Clients connect to Unix socket
    - Messages: Newline-delimited JSON
    - Heartbeat: Server sends {"type": "heartbeat"} every 30s
    - Graceful close: Server sends {"type": "shutdown"} before closing
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventServer:
    """Unix socket server for broadcasting events to UI processes.

    The server maintains a set of connected clients and broadcasts
    all events to each one. Clients that disconnect are automatically
    removed.

    The server also receives events from clients (e.g., from validation
    subprocesses via emit.py) and forwards them via the on_event callback.

    Attributes:
        socket_path: Path to the Unix socket file
        clients: Set of connected client writers
        on_event: Callback for events received from clients
    """

    def __init__(
        self,
        socket_path: str | Path | None = None,
        on_event: "Callable[[str, dict], None] | None" = None,
    ):
        """Initialize the event server.

        Args:
            socket_path: Path to Unix socket. Defaults to
                         /tmp/issue-orchestrator-{uid}.sock
            on_event: Callback when event received from client.
                      Signature: on_event(event_name, event_data)
        """
        if socket_path is None:
            socket_path = Path(f"/tmp/issue-orchestrator-{os.getuid()}.sock")
        self.socket_path = Path(socket_path)
        self.clients: set[asyncio.StreamWriter] = set()
        self._on_event = on_event
        self._server: asyncio.Server | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the event server.

        Creates the Unix socket and begins accepting connections.
        Also starts the heartbeat task to keep connections alive.
        """
        # Remove stale socket file if it exists
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path)
        )
        self._running = True

        # Make socket readable by owner only (security)
        self.socket_path.chmod(0o600)

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info(f"EventServer started on {self.socket_path}")

    async def stop(self) -> None:
        """Stop the event server gracefully.

        Sends shutdown message to all clients, closes connections,
        and removes the socket file.
        """
        self._running = False

        # Cancel heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Notify clients of shutdown
        await self.broadcast({"type": "shutdown"})

        # Close all client connections
        for writer in list(self.clients):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self.clients.clear()

        # Stop server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Remove socket file
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        logger.info("EventServer stopped")

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Broadcast an event to all connected clients.

        Args:
            event: Event dictionary to send. Must be JSON-serializable.
        """
        if not self.clients:
            return

        message = json.dumps(event) + "\n"
        data = message.encode("utf-8")

        dead_clients = []
        for writer in self.clients:
            try:
                writer.write(data)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                dead_clients.append(writer)

        # Remove dead clients
        for writer in dead_clients:
            self.clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ) -> None:
        """Handle a new client connection.

        Clients can:
        - Receive events (broadcast from server)
        - Send events (forwarded to on_event callback)

        Args:
            reader: Client stream reader for receiving events
            writer: Client stream writer for sending events
        """
        peer = writer.get_extra_info("peername") or "unknown"
        logger.info(f"EventServer: Client connected from {peer}")

        self.clients.add(writer)

        # Send welcome message
        try:
            welcome = {"type": "connected", "version": "1.0"}
            writer.write((json.dumps(welcome) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:
            logger.warning(f"EventServer: Failed to send welcome: {e}")
            self.clients.discard(writer)
            return

        # Read events from client
        buffer = ""
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=60.0)
                    if not data:
                        # Client closed connection
                        break

                    buffer += data.decode("utf-8")

                    # Process complete lines (newline-delimited JSON)
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            self._process_incoming(line)

                except asyncio.TimeoutError:
                    # No data, but connection still alive
                    continue
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"EventServer: Client disconnected from {peer}")

    def _process_incoming(self, line: str) -> None:
        """Process an incoming event from a client.

        Args:
            line: JSON-encoded event string
        """
        try:
            message = json.loads(line)

            # Events from emit.py have type="event"
            if message.get("type") == "event":
                event_name = message.get("name", "unknown")
                event_data = message.get("data", {})

                logger.debug(f"EventServer: Received event '{event_name}' from client")

                # Forward to callback if registered
                if self._on_event:
                    try:
                        self._on_event(event_name, event_data)
                    except Exception as e:
                        logger.error(f"EventServer: on_event callback failed: {e}")

                # Also broadcast to all clients (so dashboards see it too)
                asyncio.create_task(self.broadcast({
                    "type": "trace",
                    "name": event_name,
                    "data": event_data,
                }))
            else:
                logger.debug(f"EventServer: Ignoring message type '{message.get('type')}'")

        except json.JSONDecodeError as e:
            logger.warning(f"EventServer: Invalid JSON from client: {e}")

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to keep connections alive."""
        while self._running:
            try:
                await asyncio.sleep(30.0)
                if self._running:
                    await self.broadcast({"type": "heartbeat"})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"EventServer: Heartbeat error: {e}")

    @property
    def client_count(self) -> int:
        """Return the number of connected clients."""
        return len(self.clients)

    @property
    def is_running(self) -> bool:
        """Return whether the server is running."""
        return self._running

    def set_event_handler(
        self,
        handler: "Callable[[str, dict], None]",
    ) -> None:
        """Set the event handler for subprocess events.

        This allows setting the handler after construction, which is useful
        when dependencies aren't available at construction time.

        Args:
            handler: Callback when event received from client.
                     Signature: handler(event_name, event_data)
        """
        self._on_event = handler
