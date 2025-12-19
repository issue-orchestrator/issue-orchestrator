"""Tests for the IPC (Inter-Process Communication) module."""

import asyncio
import json
import os
import pytest
from pathlib import Path
import uuid

from issue_orchestrator.ipc import EventServer, EventClient


class TestEventServer:
    """Tests for the EventServer class."""

    @pytest.fixture
    def socket_path(self):
        """Create a temporary socket path in /tmp (short path for Unix socket limit)."""
        # Unix sockets have a path length limit (~104 chars on macOS)
        # Use /tmp with a short unique name
        path = Path(f"/tmp/orch-test-{uuid.uuid4().hex[:8]}.sock")
        yield path
        # Cleanup
        if path.exists():
            path.unlink()

    @pytest.mark.asyncio
    async def test_start_and_stop(self, socket_path):
        """Test that server starts and stops cleanly."""
        server = EventServer(socket_path)

        await server.start()
        assert server.is_running
        assert socket_path.exists()

        await server.stop()
        assert not server.is_running
        assert not socket_path.exists()

    @pytest.mark.asyncio
    async def test_removes_stale_socket(self, socket_path):
        """Test that server removes stale socket file on start."""
        # Create a stale socket file
        socket_path.touch()

        server = EventServer(socket_path)
        await server.start()

        assert server.is_running
        await server.stop()

    @pytest.mark.asyncio
    async def test_broadcast_with_no_clients(self, socket_path):
        """Test that broadcast works with no clients connected."""
        server = EventServer(socket_path)
        await server.start()

        # Should not raise
        await server.broadcast({"type": "test", "data": "hello"})

        assert server.client_count == 0
        await server.stop()

    @pytest.mark.asyncio
    async def test_client_receives_events(self, socket_path):
        """Test that connected client receives broadcast events."""
        server = EventServer(socket_path)
        await server.start()

        # Connect client
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))

        # Wait for welcome message
        welcome_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        welcome = json.loads(welcome_line)
        assert welcome["type"] == "connected"

        # Broadcast an event
        await server.broadcast({"type": "test_event", "value": 42})

        # Client should receive it
        event_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event = json.loads(event_line)
        assert event["type"] == "test_event"
        assert event["value"] == 42

        writer.close()
        await writer.wait_closed()
        await server.stop()

    @pytest.mark.asyncio
    async def test_multiple_clients(self, socket_path):
        """Test that multiple clients all receive events."""
        server = EventServer(socket_path)
        await server.start()

        # Connect two clients
        clients = []
        for _ in range(2):
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            # Read welcome
            await reader.readline()
            clients.append((reader, writer))

        # Small delay for server to register clients
        await asyncio.sleep(0.1)

        # Broadcast an event
        await server.broadcast({"type": "multi_test"})

        # Both clients should receive it
        for reader, writer in clients:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            event = json.loads(line)
            assert event["type"] == "multi_test"

        # Cleanup
        for reader, writer in clients:
            writer.close()
            await writer.wait_closed()
        await server.stop()

    @pytest.mark.asyncio
    async def test_dead_client_removed(self, socket_path):
        """Test that disconnected clients are removed on broadcast."""
        server = EventServer(socket_path)
        await server.start()

        # Connect and immediately disconnect
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        await reader.readline()  # welcome
        writer.close()
        await writer.wait_closed()

        # Small delay
        await asyncio.sleep(0.1)

        # Broadcast should clean up the dead client
        await server.broadcast({"type": "cleanup_test"})

        # Server should handle this gracefully
        await server.stop()


class TestEventClient:
    """Tests for the EventClient class."""

    @pytest.fixture
    def socket_path(self):
        """Create a temporary socket path in /tmp."""
        path = Path(f"/tmp/orch-test-{uuid.uuid4().hex[:8]}.sock")
        yield path
        if path.exists():
            path.unlink()

    @pytest.mark.asyncio
    async def test_connect_to_running_server(self, socket_path):
        """Test that client connects to running server."""
        server = EventServer(socket_path)
        await server.start()

        client = EventClient(socket_path)
        connected = await client.connect()

        assert connected
        assert client.is_connected

        await client.disconnect()
        await server.stop()

    @pytest.mark.asyncio
    async def test_connect_fails_when_no_server(self, socket_path):
        """Test that client fails to connect when no server."""
        client = EventClient(socket_path)
        connected = await client.connect()

        assert not connected
        assert not client.is_connected

    @pytest.mark.asyncio
    async def test_events_generator(self, socket_path):
        """Test the events async generator."""
        server = EventServer(socket_path)
        await server.start()

        client = EventClient(socket_path)
        await client.connect()

        # Start consuming events (skip connected message)
        events_received = []

        async def consume_events():
            async for event in client.events(auto_reconnect=False):
                if event.get("type") == "connected":
                    continue  # Skip welcome message
                events_received.append(event)
                if len(events_received) >= 2:
                    break

        # Start consumer task
        consumer = asyncio.create_task(consume_events())

        # Give consumer time to start
        await asyncio.sleep(0.1)

        # Broadcast some events
        await server.broadcast({"type": "event1"})
        await server.broadcast({"type": "event2"})

        # Wait for consumer to finish
        await asyncio.wait_for(consumer, timeout=2.0)

        assert len(events_received) == 2
        assert events_received[0]["type"] == "event1"
        assert events_received[1]["type"] == "event2"

        await client.disconnect()
        await server.stop()

    @pytest.mark.asyncio
    async def test_heartbeat_ignored(self, socket_path):
        """Test that heartbeat messages are not yielded."""
        server = EventServer(socket_path)
        await server.start()

        client = EventClient(socket_path)
        await client.connect()

        events_received = []

        async def consume_one_real():
            async for event in client.events(auto_reconnect=False):
                # Skip connected message
                if event.get("type") == "connected":
                    continue
                events_received.append(event)
                break

        consumer = asyncio.create_task(consume_one_real())
        await asyncio.sleep(0.1)

        # Send heartbeat followed by real event
        # Heartbeat should be filtered by the client
        await server.broadcast({"type": "heartbeat"})
        await server.broadcast({"type": "real_event"})

        await asyncio.wait_for(consumer, timeout=2.0)

        # Should only have the real event, not heartbeat
        assert len(events_received) == 1
        assert events_received[0]["type"] == "real_event"

        await client.disconnect()
        await server.stop()


class TestLifecycleIPCPlugin:
    """Tests for the LifecycleIPCPlugin."""

    @pytest.fixture
    def socket_path(self):
        """Create a temporary socket path in /tmp."""
        path = Path(f"/tmp/orch-test-{uuid.uuid4().hex[:8]}.sock")
        yield path
        if path.exists():
            path.unlink()

    @pytest.mark.asyncio
    async def test_plugin_broadcasts_events(self, socket_path):
        """Test that plugin forwards lifecycle events to IPC."""
        from issue_orchestrator.adapters import LifecycleIPCPlugin

        server = EventServer(socket_path)
        await server.start()

        plugin = LifecycleIPCPlugin(server)

        # Connect a client
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        await reader.readline()  # welcome

        # Call a lifecycle hook
        plugin.on_session_started(
            issue_number=123,
            session_id="test-session",
            worktree_path="/tmp/worktree",
            branch_name="123-test-branch",
        )

        # Client should receive the event
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event = json.loads(line)

        assert event["type"] == "session_started"
        assert event["issue_number"] == 123
        assert event["session_id"] == "test-session"

        writer.close()
        await writer.wait_closed()
        await server.stop()

    @pytest.mark.asyncio
    async def test_plugin_handles_all_lifecycle_hooks(self, socket_path):
        """Test that plugin handles all lifecycle hook types."""
        from issue_orchestrator.adapters import LifecycleIPCPlugin

        server = EventServer(socket_path)
        await server.start()

        plugin = LifecycleIPCPlugin(server)

        # Connect a client
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        await reader.readline()  # welcome

        # Test each hook type
        hooks = [
            ("on_issue_claimed", {"issue_number": 1, "title": "Test", "agent_type": "test"}),
            ("on_session_completed", {"issue_number": 1, "session_id": "s1", "pr_url": None, "runtime_minutes": 5.0}),
            ("on_session_failed", {"issue_number": 1, "session_id": "s1", "error": "fail", "runtime_minutes": 1.0}),
            ("on_issue_blocked", {"issue_number": 1, "reason": "blocked"}),
            ("on_issue_needs_human", {"issue_number": 1, "reason": "help"}),
            ("on_pr_created", {"issue_number": 1, "pr_number": 10, "pr_url": "http://...", "title": "PR"}),
            ("on_review_requested", {"pr_number": 10, "issue_number": 1, "review_type": "code"}),
            ("on_review_completed", {"pr_number": 10, "issue_number": 1, "result": "approved", "rework_count": 0}),
            ("on_review_escalated", {"pr_number": 10, "issue_number": 1, "rework_count": 4, "max_rework_cycles": 3}),
            ("on_orchestrator_state_changed", {"active_count": 2, "paused": False, "completed_today": 5}),
        ]

        for hook_name, kwargs in hooks:
            hook = getattr(plugin, hook_name)
            hook(**kwargs)

            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            event = json.loads(line)
            # Just verify it was received
            assert "type" in event

        writer.close()
        await writer.wait_closed()
        await server.stop()
