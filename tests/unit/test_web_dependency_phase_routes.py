"""Dependency and phase route tests split from test_web."""

# ruff: noqa: F403,F405,SLF001

from tests.unit import test_web as _support
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestDependencyProblemsEndpoint:
    """Test the GET /api/dependency-problems endpoint."""

    def test_get_dependency_problems_empty(self):
        """Test getting dependency problems when none exist."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import DependencyProblem

        mock_orch = create_mock_orchestrator()
        mock_orch.state.dependency_problems = {}
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.get("/api/dependency-problems")

            assert response.status_code == 200
            data = response.json()
            assert data["problems"] == {}
        finally:
            set_orchestrator(None)

    def test_get_dependency_problems_with_problems(self):
        """Test getting dependency problems when some exist."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import DependencyProblem

        mock_orch = create_mock_orchestrator()

        problem = DependencyProblem(
            issue_number=1,
            issue_title="Blocked Issue",
            blocked_by=[(2, "Dependency Issue", "open")],  # Required field
            summary="Waiting for #2 to be merged",
        )
        mock_orch.state.dependency_problems = {1: problem}
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.get("/api/dependency-problems")

            assert response.status_code == 200
            data = response.json()
            # Keys are returned as strings in JSON
            assert "1" in data["problems"] or 1 in data["problems"]
            problem_data = data["problems"].get("1") or data["problems"].get(1)
            assert problem_data["issue_number"] == 1
            assert problem_data["issue_title"] == "Blocked Issue"
            assert problem_data["summary"] == "Waiting for #2 to be merged"
        finally:
            set_orchestrator(None)

    def test_get_dependency_problems_when_orchestrator_not_running(self):
        """Test dependency-problems returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/dependency-problems")

        assert response.status_code == 503
        assert "error" in response.json()


class TestSessionPhasesEndpoint:
    """Tests for the GET /api/session/phases/{issue_number} endpoint."""

    def test_phases_returns_empty_when_no_worktree_found(self):
        """Test phases endpoint returns empty when no worktree exists for issue."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.state.active_sessions = []
        mock_orch.state.session_history = []

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/999")

            assert response.status_code == 200
            data = response.json()
            assert data["phases"] == []
            assert data["current_phase"] is None
            assert "error" in data or data.get("issue_number") == 999
        finally:
            set_orchestrator(None)

    def test_phases_returns_503_when_orchestrator_not_running(self):
        """Test phases endpoint returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/session/phases/123")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_phases_finds_worktree_from_active_session(self, tmp_path):
        """Test phases endpoint finds worktree from active session."""
        from issue_orchestrator.entrypoints import web
        import json

        mock_orch = create_mock_orchestrator()

        # Create a worktree with session data
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        run_dir = sessions_dir / "20260117-100000Z__coding-1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(json.dumps({
            "session_name": "coding-1",
            "run_id": "20260117-100000Z",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "issue_number": 123,
            "agent_label": "agent:developer",
            "outcome": "completed",
        }))

        (sessions_dir / "index.json").write_text(json.dumps({
            "runs": [{
                "session_name": "coding-1",
                "run_id": "20260117-100000Z",
                "started_at": "2026-01-17T10:00:00Z",
                "issue_number": 123,
                "run_dir": str(run_dir),
                "agent_label": "agent:developer",
            }]
        }))

        # Create an active session pointing to this worktree
        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path=str(tmp_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/123")

            assert response.status_code == 200
            data = response.json()
            assert len(data["phases"]) == 1
            assert data["phases"][0]["name"] == "coding-1"
            assert data["phases"][0]["display_name"] == "Coding 1"
            assert data["phases"][0]["status"] == "completed"
            assert data["issue_number"] == 123
        finally:
            set_orchestrator(None)

    def test_phases_formats_phase_names_correctly(self, tmp_path):
        """Test that phase names are formatted correctly for display."""
        from issue_orchestrator.entrypoints import web
        import json

        mock_orch = create_mock_orchestrator()

        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create multiple phases
        phases_data = [
            ("coding-1", "20260117-100000Z"),
            ("review-1", "20260117-110000Z"),
            ("coding-2", "20260117-120000Z"),
        ]

        runs_index = []
        for phase_name, run_id in phases_data:
            run_dir = sessions_dir / f"{run_id}__{phase_name}"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(json.dumps({
                "session_name": phase_name,
                "run_id": run_id,
                "started_at": f"2026-01-17T{run_id[9:11]}:00:00Z",
                "ended_at": f"2026-01-17T{run_id[9:11]}:30:00Z",
                "outcome": "completed",
            }))
            runs_index.append({
                "session_name": phase_name,
                "run_id": run_id,
                "started_at": f"2026-01-17T{run_id[9:11]}:00:00Z",
                "run_dir": str(run_dir),
            })

        (sessions_dir / "index.json").write_text(json.dumps({"runs": runs_index}))

        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path=str(tmp_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/123")

            assert response.status_code == 200
            data = response.json()
            assert len(data["phases"]) == 3
            assert data["phases"][0]["display_name"] == "Coding 1"
            assert data["phases"][1]["display_name"] == "Review 1"
            assert data["phases"][2]["display_name"] == "Coding 2"
        finally:
            set_orchestrator(None)

    def test_phases_identifies_current_in_progress_phase(self, tmp_path):
        """Test that current_phase is set for in_progress phases."""
        from issue_orchestrator.entrypoints import web
        import json

        mock_orch = create_mock_orchestrator()

        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create one completed and one in-progress phase
        run1_dir = sessions_dir / "20260117-100000Z__coding-1"
        run1_dir.mkdir()
        (run1_dir / "manifest.json").write_text(json.dumps({
            "session_name": "coding-1",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "outcome": "completed",
        }))

        run2_dir = sessions_dir / "20260117-110000Z__review-1"
        run2_dir.mkdir()
        (run2_dir / "manifest.json").write_text(json.dumps({
            "session_name": "review-1",
            "started_at": "2026-01-17T11:00:00Z",
            # No ended_at - still in progress
        }))

        (sessions_dir / "index.json").write_text(json.dumps({
            "runs": [
                {"session_name": "coding-1", "run_id": "20260117-100000Z",
                 "started_at": "2026-01-17T10:00:00Z", "run_dir": str(run1_dir)},
                {"session_name": "review-1", "run_id": "20260117-110000Z",
                 "started_at": "2026-01-17T11:00:00Z", "run_dir": str(run2_dir)},
            ]
        }))

        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path=str(tmp_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/123")

            assert response.status_code == 200
            data = response.json()
            assert data["current_phase"] == "review-1"
            assert data["phases"][1]["status"] == "in_progress"
        finally:
            set_orchestrator(None)


def _get_available_port() -> int:
    """Get an available port by binding to port 0 and releasing it."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPortUtilityFunctions:
    """Test port utility functions."""

    def test_is_port_in_use_when_available(self):
        """Test port check returns False for available port."""
        from issue_orchestrator.entrypoints.web import _is_port_in_use

        # Get a dynamically allocated available port
        port = _get_available_port()
        result = _is_port_in_use(port)
        assert result is False

    def test_is_port_in_use_when_bound(self):
        """Test port check returns True when port is bound."""
        import socket
        from issue_orchestrator.entrypoints.web import _is_port_in_use

        # Bind to a dynamic port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        try:
            result = _is_port_in_use(port, "127.0.0.1")
            assert result is True
        finally:
            sock.close()

    def test_kill_process_on_port_no_process(self):
        """Test killing process on port when no process exists."""
        from issue_orchestrator.entrypoints.web import _kill_process_on_port

        # Mock lsof to return empty (no process found) — avoids TOCTOU race
        # where a released dynamic port gets grabbed by another process
        # between allocation and the lsof check.
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("issue_orchestrator.entrypoints.web.subprocess.run", return_value=mock_result):
            result = _kill_process_on_port(9999)
        assert result is False

    def test_ensure_port_available_when_available(self):
        """Test ensure_port_available succeeds when port is available."""
        from issue_orchestrator.entrypoints.web import ensure_port_available

        # Get a dynamically allocated available port
        port = _get_available_port()
        # Should not raise
        ensure_port_available(port)

    def test_ensure_port_available_when_unavailable(self):
        """Test ensure_port_available raises when port cannot be freed."""
        import socket
        from issue_orchestrator.entrypoints.web import ensure_port_available

        # Bind to a dynamic port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        try:
            with patch("issue_orchestrator.entrypoints.web._kill_process_on_port", return_value=False):
                with patch("time.sleep", return_value=None):
                    with pytest.raises(RuntimeError, match="Port .* is already in use"):
                        ensure_port_available(port)
        finally:
            sock.close()
