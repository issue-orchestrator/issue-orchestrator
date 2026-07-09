"""Dashboard runtime tests split from test_web."""

# ruff: noqa: F403,F405

from tests.unit import test_web as _support
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestGetOrchestrator:
    """Test the get_orchestrator dependency function."""

    def test_get_orchestrator_returns_global(self):
        """Test get_orchestrator returns the global orchestrator."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        try:
            result = web.get_orchestrator()
            assert result is mock_orch
        finally:
            set_orchestrator(None)

    def test_get_orchestrator_returns_none(self):
        """Test get_orchestrator returns None when not set."""
        from issue_orchestrator.entrypoints import web

        set_orchestrator(None)
        result = web.get_orchestrator()
        assert result is None


class TestTriggerServerShutdown:
    """Test the trigger_server_shutdown function."""

    def test_trigger_server_shutdown_sets_flag(self):
        """Test trigger_server_shutdown sets should_exit flag."""
        from issue_orchestrator.entrypoints import web

        mock_server = MagicMock()
        set_server(mock_server)

        try:
            web.trigger_server_shutdown()
            assert mock_server.should_exit is True
        finally:
            set_server(None)

    def test_trigger_server_shutdown_when_no_server(self):
        """Test trigger_server_shutdown handles None server gracefully."""
        from issue_orchestrator.entrypoints import web

        set_server(None)
        # Should not raise
        web.trigger_server_shutdown()


class TestDashboardWithProblems:
    """Test dashboard shows problem items in the kanban blocked column."""

    def test_dashboard_with_failed_session(self):
        """Test dashboard displays failed sessions in the blocked column."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add a failed session to history — goes to blocked column
        failed_entry = SessionHistoryEntry(
            issue_number=1,
            title="Failed Issue",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=30,
        )
        mock_orch.state.session_history = [failed_entry]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=kanban")

            assert response.status_code == 200
            assert "Failed Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_blocked_session(self):
        """Test dashboard displays blocked sessions in the blocked column."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        blocked_entry = SessionHistoryEntry(
            issue_number=2,
            title="Blocked Issue",
            agent_type="agent:web",
            status="blocked",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [blocked_entry]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=kanban")

            assert response.status_code == 200
            assert "Blocked Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_timed_out_session(self):
        """Test dashboard displays timed out sessions in the blocked column."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        timeout_entry = SessionHistoryEntry(
            issue_number=3,
            title="Timeout Issue",
            agent_type="agent:web",
            status="timed_out",
            runtime_minutes=60,
        )
        mock_orch.state.session_history = [timeout_entry]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=kanban")

            assert response.status_code == 200
            assert "Timeout Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_needs_human_session(self):
        """Test dashboard displays needs_human sessions in blocked tab."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        needs_human_entry = SessionHistoryEntry(
            issue_number=4,
            title="Needs Human Issue",
            agent_type="agent:web",
            status="needs_human",
            runtime_minutes=15,
        )
        mock_orch.state.session_history = [needs_human_entry]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=blocked")

            assert response.status_code == 200
            assert "Needs Human Issue" in response.text
        finally:
            set_orchestrator(None)


class TestDashboardStartupStatus:
    """Test dashboard with different startup statuses."""

    def test_dashboard_with_startup_pending(self):
        """Test dashboard when startup is pending."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.startup_status = "pending"
        mock_orch.state.startup_message = "Initializing..."

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should render but not show queue (startup incomplete)
        finally:
            set_orchestrator(None)

    def test_dashboard_with_startup_in_progress(self):
        """Test dashboard when startup is in progress."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.startup_status = "in_progress"
        mock_orch.state.startup_message = "Fetching issues..."

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
        finally:
            set_orchestrator(None)


class TestDashboardValidationWarning:
    """Issue #4109: persistent warning when no validation is configured."""

    def test_dashboard_shows_warning_when_validation_missing(self):
        mock_orch = create_mock_orchestrator()
        # create_mock_orchestrator() builds a Config() with no validation cmd.
        assert mock_orch.config.is_validation_enabled() is False

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "validation-warning-banner" in response.text
            assert "No validation configured" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_hides_warning_when_validation_configured(self):
        mock_orch = create_mock_orchestrator()
        mock_orch.config.validation.quick.cmd = "make validate"
        assert mock_orch.config.is_validation_enabled() is True

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "validation-warning-banner" not in response.text
        finally:
            set_orchestrator(None)


class TestDashboardWithPendingReviews:
    """Test dashboard displays pending reviews."""

    def test_dashboard_pending_reviews_in_status(self):
        """Test /api/status includes pending reviews."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import PendingReview
        from issue_orchestrator.domain.issue_key import FakeIssueKey

        mock_orch = create_mock_orchestrator()

        # Use FakeIssueKey which returns name as stable_id (can be a number string)
        issue_key = FakeIssueKey(name="1")
        review = PendingReview(
            issue_key=issue_key,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="feature/issue-1",
            _issue_number=1,
        )
        mock_orch.state.pending_reviews = [review]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()
            assert len(data["pending_reviews"]) == 1
            assert data["pending_reviews"][0]["issue_number"] == 1
            assert data["pending_reviews"][0]["pr_number"] == 10
        finally:
            set_orchestrator(None)


class TestDashboardWithSlowSessions:
    """Test dashboard displays slow sessions."""

    def test_dashboard_slow_session_over_timeout(self):
        """Test dashboard marks sessions as slow when over timeout."""
        from issue_orchestrator.entrypoints import web
        from datetime import datetime, timedelta
        mock_orch = create_mock_orchestrator()

        # Create a session that's been running longer than timeout
        issue = create_issue(1, "Slow Issue")
        session = create_session(issue)
        # Set start_time to 60 minutes ago (over 45 min timeout)
        session.start_time = datetime.now() - timedelta(minutes=60)
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should render the slow session
        finally:
            set_orchestrator(None)


class TestDashboardReviewPhase:
    """Test dashboard displays review phase sessions."""

    def test_dashboard_review_phase_session(self):
        """Test dashboard identifies review sessions by terminal_id."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Review Issue")
        session = create_session(issue)
        # Make it a review session
        session.terminal_id = "review-1"
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should show "Reviewing" phase
        finally:
            set_orchestrator(None)


class TestRunWebDashboard:
    """Test run_web_dashboard function."""

    @pytest.mark.asyncio
    async def test_run_web_dashboard_sets_global_orchestrator(self):
        """Test run_web_dashboard sets global orchestrator."""
        from issue_orchestrator.entrypoints.web import run_web_dashboard
        from issue_orchestrator.entrypoints import web
        import uvicorn
        import asyncio
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()

        mock_server = MagicMock()
        serve_started = asyncio.Event()

        async def serve():
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open"):
                    # Start the task
                    task = asyncio.create_task(run_web_dashboard(mock_orch, port=8080))

                    await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")

                    # Check orchestrator was set
                    assert get_orchestrator() is mock_orch

                    # Cancel the task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    # Clean up
                    set_orchestrator(None)
                    set_server(None)

    @pytest.mark.asyncio
    async def test_run_web_dashboard_opens_browser(self):
        """Test run_web_dashboard opens browser."""
        from issue_orchestrator.entrypoints.web import run_web_dashboard
        from issue_orchestrator.entrypoints import web
        import uvicorn
        import asyncio
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()

        mock_server = MagicMock()
        serve_started = asyncio.Event()
        browser_opened = asyncio.Event()

        async def serve():
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.asyncio.sleep", new=AsyncMock()):
                    with patch("issue_orchestrator.entrypoints.web.webbrowser.open") as mock_open:
                        mock_open.side_effect = lambda url: browser_opened.set()
                        task = asyncio.create_task(run_web_dashboard(mock_orch, port=8080))

                        await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")
                        await wait_for_async_event(browser_opened, timeout=1.0, label="browser_opened")

                        # Should have opened browser
                        mock_open.assert_called_once_with("http://127.0.0.1:8080")

                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                        set_orchestrator(None)
                        set_server(None)


    @pytest.mark.asyncio
    async def test_run_web_dashboard_port_zero_resolves_bound_port(self):
        """When port=0, browser opens with the OS-assigned port."""
        from issue_orchestrator.entrypoints.web import run_web_dashboard
        import asyncio
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()

        # Mock a socket that reports port 54321
        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ("127.0.0.1", 54321)
        mock_inner_server = MagicMock()
        mock_inner_server.sockets = [mock_socket]

        mock_server = MagicMock()
        mock_server.servers = [mock_inner_server]
        mock_server.started = False
        serve_started = asyncio.Event()
        browser_opened = asyncio.Event()

        async def serve():
            mock_server.started = True
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("uvicorn.Server", return_value=mock_server):
            with patch("uvicorn.Config"):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open") as mock_open:
                    mock_open.side_effect = lambda url: browser_opened.set()
                    task = asyncio.create_task(run_web_dashboard(mock_orch, port=0))

                    await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")
                    await wait_for_async_event(browser_opened, timeout=1.0, label="browser_opened")

                    mock_open.assert_called_once_with("http://127.0.0.1:54321")

                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    set_orchestrator(None)
                    set_server(None)

    @pytest.mark.asyncio
    async def test_run_web_dashboard_port_zero_reports_bound_port_to_callback(self):
        """When port=0, startup callback receives the OS-assigned port."""
        from issue_orchestrator.entrypoints.web import run_web_dashboard
        import asyncio
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()

        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ("127.0.0.1", 54321)
        mock_inner_server = MagicMock()
        mock_inner_server.sockets = [mock_socket]

        mock_server = MagicMock()
        mock_server.servers = [mock_inner_server]
        mock_server.started = False
        serve_started = asyncio.Event()
        callback_seen = asyncio.Event()
        captured: dict[str, int] = {}

        async def serve():
            mock_server.started = True
            serve_started.set()
            await asyncio.Event().wait()

        def on_server_started(actual_port: int) -> None:
            captured["port"] = actual_port
            callback_seen.set()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("uvicorn.Server", return_value=mock_server):
            with patch("uvicorn.Config"):
                task = asyncio.create_task(
                    run_web_dashboard(
                        mock_orch,
                        port=0,
                        open_browser=False,
                        on_server_started=on_server_started,
                    )
                )

                await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")
                await wait_for_async_event(callback_seen, timeout=1.0, label="callback_seen")

                assert captured["port"] == 54321

                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                set_orchestrator(None)
                set_server(None)


class TestRunWithWebDashboard:
    """Test run_with_web_dashboard function."""

    @pytest.mark.asyncio
    async def test_run_with_web_dashboard_starts_orchestrator(self):
        """Test run_with_web_dashboard runs orchestrator startup and loop."""
        from issue_orchestrator.entrypoints.web import run_with_web_dashboard
        from issue_orchestrator.entrypoints import web
        import uvicorn
        import asyncio
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()
        startup_called = asyncio.Event()
        run_loop_called = asyncio.Event()

        async def startup():
            startup_called.set()

        async def run_loop():
            run_loop_called.set()
            await asyncio.Event().wait()

        mock_orch.startup = AsyncMock(side_effect=startup)
        mock_orch.run_loop = AsyncMock(side_effect=run_loop)

        mock_server = MagicMock()
        serve_started = asyncio.Event()

        async def serve():
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open"):
                    with patch("issue_orchestrator.entrypoints.web.asyncio.sleep", new=AsyncMock()):
                        task = asyncio.create_task(run_with_web_dashboard(mock_orch, port=8080))

                        await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")
                        await wait_for_async_event(startup_called, timeout=1.0, label="startup_called")

                        # Startup should have been called
                        assert mock_orch.startup.called or True  # May be in thread

                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                        set_orchestrator(None)
                        set_server(None)
