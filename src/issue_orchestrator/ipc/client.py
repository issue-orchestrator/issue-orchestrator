"""Unix socket event client for receiving lifecycle events.

The EventClient connects to the orchestrator's EventServer and
receives real-time event notifications. It handles reconnection
automatically if the connection is lost.

Usage:
    client = EventClient()
    await client.connect()

    async for event in client.events():
        if event["type"] == "session_completed":
            print(f"Session completed: {event}")
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class EventClient:
    """Unix socket client for receiving events from the orchestrator.

    The client connects to the orchestrator's event server and yields
    events as they arrive. It automatically handles reconnection if
    the connection is lost.

    Attributes:
        socket_path: Path to the Unix socket file
        reconnect_delay: Seconds to wait before reconnecting
    """

    def __init__(
        self,
        socket_path: str | Path | None = None,
        reconnect_delay: float = 2.0
    ):
        """Initialize the event client.

        Args:
            socket_path: Path to Unix socket. Defaults to
                         /tmp/issue-orchestrator-{uid}.sock
            reconnect_delay: Seconds to wait before reconnecting
        """
        if socket_path is None:
            socket_path = Path(f"/tmp/issue-orchestrator-{os.getuid()}.sock")
        self.socket_path = Path(socket_path)
        self.reconnect_delay = reconnect_delay
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    async def connect(self) -> bool:
        """Connect to the event server.

        Returns:
            True if connected successfully, False otherwise.
        """
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                path=str(self.socket_path)
            )
            self._connected = True
            logger.info(f"EventClient: Connected to {self.socket_path}")
            return True
        except (FileNotFoundError, ConnectionRefusedError) as e:
            logger.warning(f"EventClient: Connection failed: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from the event server."""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None
        logger.info("EventClient: Disconnected")

    async def events(self, auto_reconnect: bool = True) -> AsyncIterator[dict[str, Any]]:
        """Yield events from the server as they arrive.

        This is an async generator that yields event dictionaries.
        It handles reconnection automatically if the connection is lost.

        Args:
            auto_reconnect: If True, automatically reconnect on disconnect

        Yields:
            Event dictionaries from the server
        """
        while True:
            # Connect if not connected
            if not self._connected:
                if not await self.connect():
                    if auto_reconnect:
                        await asyncio.sleep(self.reconnect_delay)
                        continue
                    else:
                        return

            # Read events
            try:
                async for event in self._read_events():
                    if event.get("type") == "shutdown":
                        logger.info("EventClient: Server shutting down")
                        await self.disconnect()
                        if auto_reconnect:
                            await asyncio.sleep(self.reconnect_delay)
                            break
                        else:
                            return
                    elif event.get("type") == "heartbeat":
                        # Ignore heartbeats
                        continue
                    else:
                        yield event
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                logger.warning(f"EventClient: Connection lost: {e}")
                await self.disconnect()
                if auto_reconnect:
                    await asyncio.sleep(self.reconnect_delay)
                else:
                    return

    async def _read_events(self) -> AsyncIterator[dict[str, Any]]:
        """Read and parse events from the socket.

        Yields:
            Parsed event dictionaries
        """
        if not self._reader:
            return

        buffer = ""
        while self._connected:
            try:
                data = await self._reader.read(4096)
                if not data:
                    # Connection closed
                    break

                buffer += data.decode("utf-8")

                # Process complete lines
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        try:
                            event = json.loads(line)
                            yield event
                        except json.JSONDecodeError as e:
                            logger.warning(f"EventClient: Invalid JSON: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"EventClient: Read error: {e}")
                break

    @property
    def is_connected(self) -> bool:
        """Return whether the client is connected."""
        return self._connected


async def wait_for_orchestrator(
    socket_path: str | Path | None = None,
    timeout: float = 30.0
) -> bool:
    """Wait for the orchestrator's event server to become available.

    This is useful for UI processes that start before the orchestrator.

    Args:
        socket_path: Path to Unix socket
        timeout: Maximum seconds to wait

    Returns:
        True if server became available, False if timed out
    """
    if socket_path is None:
        socket_path = Path(f"/tmp/issue-orchestrator-{os.getuid()}.sock")

    socket_path = Path(socket_path)
    start = asyncio.get_event_loop().time()

    while (asyncio.get_event_loop().time() - start) < timeout:
        if socket_path.exists():
            # Try to connect
            client = EventClient(socket_path)
            if await client.connect():
                await client.disconnect()
                return True
        await asyncio.sleep(0.5)

    return False
