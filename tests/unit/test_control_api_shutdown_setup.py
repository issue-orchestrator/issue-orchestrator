"""Shutdown and setup control route tests split from test_control_api."""

# ruff: noqa: F403,F405

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestControlCenterShutdownEndpoint:
    """Test /control/shutdown force-stop options."""

    def test_shutdown_does_not_stop_engines_when_not_requested(self):
        mock_supervisor = MagicMock()
        set_supervisor(mock_supervisor)
        try:
            with patch("threading.Thread") as mock_thread:
                client = TestClient(control_app)
                response = client.post("/control/shutdown", json={"stop_orchestrators": False})

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            mock_supervisor.stop.assert_not_called()
            mock_thread.assert_called_once()
        finally:
            set_supervisor(DefaultSupervisorOps())

    def test_shutdown_force_stops_running_engines_when_requested(self):
        from issue_orchestrator.entrypoints import control_api

        mock_supervisor = MagicMock()
        mock_supervisor.status.return_value = SimpleNamespace(state="running")
        mock_supervisor.stop_all_instances.return_value = 1
        set_supervisor(mock_supervisor)
        repos = [SimpleNamespace(path="/tmp/repo-a")]
        try:
            with patch.object(control_api, "_schedule_control_center_exit", return_value=None):
                with patch("issue_orchestrator.infra.repo_registry.list_repos", return_value=repos):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("threading.Thread") as mock_thread:
                            client = TestClient(control_app)
                            response = client.post(
                                "/control/shutdown",
                                json={"stop_orchestrators": True, "force_orchestrators": True},
                            )
                            # Worker runs in background thread; execute target inline for deterministic assertions.
                            target = mock_thread.call_args.kwargs.get("target")
                            assert callable(target)
                            target()

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            assert data["stopped_orchestrators"] == []
            mock_supervisor.stop_all_instances.assert_called_once()
            stop_args, stop_kwargs = mock_supervisor.stop_all_instances.call_args
            assert str(stop_args[0]) == "/tmp/repo-a"
            assert stop_kwargs["force"] is True
            assert stop_kwargs["force_if_graceful_fails"] is True
            assert stop_kwargs["graceful_timeout_seconds"] == 120
            mock_thread.assert_called_once()
        finally:
            set_supervisor(DefaultSupervisorOps())

    def test_shutdown_marks_failed_when_running_engine_cannot_be_stopped(self):
        from issue_orchestrator.entrypoints import control_api

        mock_supervisor = MagicMock()
        mock_supervisor.status.return_value = SimpleNamespace(state="running")
        mock_supervisor.stop_all_instances.return_value = 0
        set_supervisor(mock_supervisor)
        repos = [SimpleNamespace(path="/tmp/repo-a")]
        try:
            with patch.object(control_api, "_schedule_control_center_exit", return_value=None) as schedule_exit:
                with patch("issue_orchestrator.infra.repo_registry.list_repos", return_value=repos):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("threading.Thread") as mock_thread:
                            client = TestClient(control_app)
                            response = client.post(
                                "/control/shutdown",
                                json={"stop_orchestrators": True, "force_orchestrators": True},
                            )
                            target = mock_thread.call_args.kwargs.get("target")
                            assert callable(target)
                            target()

            assert response.status_code == 200
            op = control_api_shutdown_state.snapshot_shutdown_ops()["global_shutdown"]
            assert op is not None
            assert op["state"] == "failed"
            assert op["failed_orchestrators"] == ["/tmp/repo-a"]
            schedule_exit.assert_not_called()
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()
            set_supervisor(DefaultSupervisorOps())

    def test_shutdown_abort_is_honored_after_current_repo_attempt(self):
        from issue_orchestrator.entrypoints import control_api

        mock_supervisor = MagicMock()
        mock_supervisor.status.return_value = SimpleNamespace(state="running")
        mock_supervisor.stop_all_instances.return_value = 0
        set_supervisor(mock_supervisor)
        repos = [SimpleNamespace(path="/tmp/repo-a")]
        try:
            with patch.object(control_api, "_schedule_control_center_exit", return_value=None):
                with patch("issue_orchestrator.infra.repo_registry.list_repos", return_value=repos):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("threading.Thread") as mock_thread:
                            client = TestClient(control_app)
                            response = client.post(
                                "/control/shutdown",
                                json={"stop_orchestrators": True, "force_orchestrators": False},
                            )
                            assert response.status_code == 200
                            abort = client.post("/control/shutdown/abort")
                            assert abort.status_code == 200
                            target = mock_thread.call_args.kwargs.get("target")
                            assert callable(target)
                            target()

            op = control_api_shutdown_state.snapshot_shutdown_ops()["global_shutdown"]
            assert op is not None
            assert op["state"] == "aborted"
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()
            set_supervisor(DefaultSupervisorOps())

    def test_shutdown_reports_superseded_engine_shutdowns(self):
        mock_supervisor = MagicMock()
        set_supervisor(mock_supervisor)
        try:
            with patch("issue_orchestrator.infra.repo_registry.list_repos", return_value=[]):
                with patch("threading.Thread") as mock_thread:
                    control_api_shutdown_state.begin_engine_shutdown_operation(
                        Path("/tmp/repo-a"),
                        force=False,
                        force_if_timeout=False,
                        graceful_timeout_seconds=2,
                    )
                    client = TestClient(control_app)
                    response = client.post(
                        "/control/shutdown",
                        json={"stop_orchestrators": True, "force_orchestrators": False},
                    )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            assert data["superseded_engine_shutdowns"] == ["/tmp/repo-a"]
            mock_thread.assert_called_once()
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()
            set_supervisor(DefaultSupervisorOps())

    def test_shutdown_state_endpoint_returns_global_operation(self):
        try:
            begin_result = control_api_shutdown_state.begin_global_shutdown_operation(
                stop_orchestrators=True,
                force_orchestrators=False,
                graceful_timeout_seconds=2,
            )
            assert not isinstance(begin_result, control_api_shutdown_state.GlobalShutdownConflict)
            operation_id, _ = begin_result
            client = TestClient(control_app)
            response = client.get("/control/shutdown/state")
            assert response.status_code == 200
            data = response.json()
            assert data["global_shutdown"]["operation_id"] == operation_id
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()

    def test_shutdown_control_endpoints_update_state(self):
        try:
            begin_result = control_api_shutdown_state.begin_global_shutdown_operation(
                stop_orchestrators=True,
                force_orchestrators=False,
                graceful_timeout_seconds=2,
            )
            assert not isinstance(begin_result, control_api_shutdown_state.GlobalShutdownConflict)

            client = TestClient(control_app)
            update = client.post("/control/shutdown/update", json={"graceful_timeout_seconds": 30})
            force = client.post("/control/shutdown/force")
            abort = client.post("/control/shutdown/abort")

            assert update.status_code == 200
            assert force.status_code == 200
            assert abort.status_code == 200
            op = control_api_shutdown_state.snapshot_shutdown_ops()["global_shutdown"]
            assert op is not None
            assert op["graceful_timeout_seconds"] == 30
            assert op["force_orchestrators"] is True
            assert op["force_now_requested"] is True
            assert op["abort_requested"] is True
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()


class TestControlCenterSetupRoutes:
    """Test extracted setup-wizard route behavior."""

    def test_setup_preview_returns_raw_yaml_without_header(self):
        """Preview should preserve the legacy raw-YAML response."""
        client = TestClient(control_app)

        response = client.post(
            "/control/setup/preview",
            json={"config": {"repo": {"name": "owner/repo"}, "agents": {}}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Issue Orchestrator Configuration" not in data["yaml"]
        assert data["yaml"].startswith("repo:\n  name: owner/repo\n")
        assert data["files"][0]["size"] == len(data["yaml"])

    def test_setup_detect_ignores_non_default_config_files(self, tmp_path):
        """Detect should only surface the legacy default config file."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        config_dir = repo_root / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "custom.yaml").write_text("repo:\n  name: owner/repo\n")

        client = TestClient(control_app)
        response = client.get(
            "/control/setup/detect",
            params={"repo_root": str(repo_root)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["config_path"] is None
        assert data["existing_config"] is None

    def test_setup_save_preserves_legacy_labels_and_raw_yaml(self, tmp_path):
        """Save should keep the old label set and config-file format."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        host = MagicMock()
        host.list_labels.return_value = []

        with patch(
            "issue_orchestrator.execution.providers.create_repository_host",
            return_value=host,
        ):
            client = TestClient(control_app)
            response = client.post(
                "/control/setup/save",
                json={
                    "repo_root": str(repo_root),
                    "config_name": "default",
                    "create_prompts": False,
                    "create_labels": True,
                    "config": {
                        "repo": {"name": "owner/repo"},
                        "agents": {
                            "agent:backend": {"prompt": ".prompts/backend.md"},
                        },
                        "review": {"enabled": True},
                    },
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "priority:high" not in data["created_labels"]
        assert "needs-code-review" in data["created_labels"]
        assert "code-reviewed" in data["created_labels"]

        config_path = repo_root / ".issue-orchestrator" / "config" / "default.yaml"
        config_text = config_path.read_text()
        assert "Issue Orchestrator Configuration" not in config_text
        assert config_text.startswith("repo:\n  name: owner/repo\n")
