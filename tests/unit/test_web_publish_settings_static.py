"""Publish job, settings, static, and audit route tests split from test_web."""

# ruff: noqa: F403,F405,SLF001

from tests.unit import test_web as _support
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestPublishJobsEndpoint:
    """Test the GET /api/publish-jobs endpoint."""

    def test_returns_empty_when_no_jobs(self):
        """Test endpoint returns empty list when no jobs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        # Create mock executor with empty history
        mock_executor = MagicMock()
        mock_executor.get_job_history.return_value = []

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/publish-jobs")

            assert response.status_code == 200
            data = response.json()
            assert data["jobs"] == []
            assert data["count"] == 0
        finally:
            web._orchestrator = None

    def test_returns_job_history(self):
        """Test endpoint returns job history with details."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.control.job_store import JobRecord

        mock_orch = create_mock_orchestrator()

        # Create mock job record
        job_record = JobRecord(
            job_id="job-123",
            issue_number=42,
            session_key="code:42",
            worktree_path="/path/to/worktree",
            worktree_id="wt-abc123",
            branch_name="issue-42-fix",
            status="succeeded",
            created_at=1000.0,
            started_at=1010.0,
            finished_at=1050.0,
            pr_url="https://github.com/owner/repo/pull/100",
            pr_number=100,
        )

        mock_executor = MagicMock()
        mock_executor.get_job_history.return_value = [job_record]

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/publish-jobs")

            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1

            job = data["jobs"][0]
            assert job["job_id"] == "job-123"
            assert job["issue_number"] == 42
            assert job["status"] == "succeeded"
            assert job["pr_url"] == "https://github.com/owner/repo/pull/100"
            assert job["pr_number"] == 100
            assert job["duration_seconds"] == 40.0
        finally:
            web._orchestrator = None

    def test_filters_by_issue_number(self):
        """Test endpoint filters by issue_number query param."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        mock_executor = MagicMock()
        mock_executor.get_job_history.return_value = []

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/publish-jobs?issue_number=42")

            assert response.status_code == 200
            # Verify filter was passed to executor
            mock_executor.get_job_history.assert_called_once_with(
                issue_number=42, limit=100
            )
        finally:
            web._orchestrator = None

    def test_returns_503_when_orchestrator_not_running(self):
        """Test endpoint returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None

        client = TestClient(app)
        response = client.get("/api/publish-jobs")

        assert response.status_code == 503
        assert "error" in response.json()


class TestApiStatusPublishJobs:
    """Test publish jobs included in /api/status endpoint."""

    def test_status_includes_publish_job_stats(self):
        """Test status endpoint includes publish job stats."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        # Create mock executor
        mock_executor = MagicMock()
        mock_executor.get_running_jobs.return_value = []
        mock_executor.get_running_count.return_value = 2
        mock_executor.get_pending_count.return_value = 3

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()

            assert "publish_job_stats" in data
            assert data["publish_job_stats"]["running"] == 2
            assert data["publish_job_stats"]["pending"] == 3
        finally:
            web._orchestrator = None

    def test_status_includes_running_publish_jobs(self):
        """Test status endpoint includes running publish jobs."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import PublishJob, PublishJobStatus

        mock_orch = create_mock_orchestrator()

        # Create a running job
        running_job = PublishJob(
            job_id="running-job-1",
            issue_number=42,
            session_key="code:42",
            status=PublishJobStatus.RUNNING,
            started_at=1000.0,
        )

        mock_executor = MagicMock()
        mock_executor.get_running_jobs.return_value = [running_job]
        mock_executor.get_running_count.return_value = 1
        mock_executor.get_pending_count.return_value = 0

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()

            assert "publish_jobs" in data
            assert len(data["publish_jobs"]) == 1
            assert data["publish_jobs"][0]["job_id"] == "running-job-1"
            assert data["publish_jobs"][0]["issue_number"] == 42
            assert data["publish_jobs"][0]["status"] == "running"
        finally:
            web._orchestrator = None


class TestSettingsEndpoints:
    """Tests for the settings page and API endpoints.

    The settings API uses a Pydantic schema-driven approach. Each tab
    (concurrency, e2e, filtering, review, hooks, advanced, goal_pilot) is a separate key
    in the request/response JSON.
    """

    def test_get_settings_returns_current_config(self):
        """GET /api/settings returns current config values grouped by tab."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.max_concurrent_sessions = 5
        mock_orch.config.e2e.enabled = True
        mock_orch.config.e2e.auto_run_interval_minutes = 45

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()

            # Tab-based structure (not nested category structure)
            assert data["concurrency"]["max_concurrent_sessions"] == 5
            assert data["e2e"]["enabled"] is True
            assert data["e2e"]["auto_run_interval_minutes"] == 45
        finally:
            web._orchestrator = None

    def test_get_settings_returns_all_tabs(self):
        """GET /api/settings returns all tabs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()
            assert set(data.keys()) == {
                "concurrency",
                "e2e",
                "filtering",
                "milestones",
                "review",
                "hooks",
                "advanced",
                "goal_pilot",
            }
        finally:
            web._orchestrator = None

    def test_get_settings_returns_503_when_orchestrator_not_running(self):
        """GET /api/settings returns 503 when orchestrator not running."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None
        client = TestClient(app)
        response = client.get("/api/settings")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_post_settings_updates_config(self):
        """POST /api/settings updates in-memory config via Pydantic schema."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.max_concurrent_sessions = 3

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 7,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True

                # Verify config was updated
                assert mock_orch.config.max_concurrent_sessions == 7
            finally:
                web._orchestrator = None

    def test_post_settings_updates_multiple_tabs(self):
        """POST /api/settings can update multiple tabs at once."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 10,
                        "session_timeout_minutes": 90,
                        "queue_refresh_seconds": 300,
                    },
                    "e2e": {
                        "enabled": True,
                        "auto_run_interval_minutes": 15,
                        "role": "executor",
                        "pytest_args": "tests/e2e -v",
                        "allow_retry_once": False,
                        "stop_on_first_failure": True,
                        "quarantine_file": "quarantine.txt",
                    },
                })

                assert response.status_code == 200
                assert mock_orch.config.max_concurrent_sessions == 10
                assert mock_orch.config.e2e.enabled is True
                assert mock_orch.config.e2e.role == "executor"
            finally:
                web._orchestrator = None

    def test_post_settings_reverts_on_validation_failure(self):
        """POST /api/settings reverts in-memory changes if doctor fails."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        original_value = mock_orch.config.max_concurrent_sessions

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_check = MagicMock()
            mock_check.status = "error"
            mock_check.name = "Test Check"
            mock_check.detail = "Validation failed"
            mock_result = MagicMock()
            mock_result.checks = [mock_check]
            mock_doctor.return_value = mock_result

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 15,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 400
                data = response.json()
                assert "error" in data
                assert "errors" in data

                # Verify config was reverted
                assert mock_orch.config.max_concurrent_sessions == original_value
            finally:
                web._orchestrator = None

    def test_post_settings_returns_warnings(self):
        """POST /api/settings includes warnings in response."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_warning = MagicMock()
            mock_warning.status = "warning"
            mock_warning.name = "Token Scope"
            mock_warning.detail = "Token has broad permissions"
            mock_result = MagicMock()
            mock_result.checks = [mock_warning]
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 5,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
                assert len(data["warnings"]) == 1
                assert data["warnings"][0]["name"] == "Token Scope"
                assert data["warnings"][0]["detail"] == "Token has broad permissions"
            finally:
                web._orchestrator = None

    def test_post_settings_rejects_invalid_values_via_pydantic(self):
        """POST /api/settings rejects out-of-range values via Pydantic validation."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            # max_concurrent_sessions has ge=1 constraint
            response = client.post("/api/settings", json={
                "concurrency": {
                    "max_concurrent_sessions": 0,
                    "session_timeout_minutes": 45,
                    "queue_refresh_seconds": 600,
                }
            })

            assert response.status_code == 400
            data = response.json()
            assert "error" in data
        finally:
            web._orchestrator = None

    def test_post_settings_rejects_invalid_enum(self):
        """POST /api/settings rejects invalid enum values."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.post("/api/settings", json={
                "e2e": {
                    "enabled": False,
                    "auto_run_interval_minutes": 30,
                    "role": "invalid_role",
                    "pytest_args": "tests/e2e -v",
                    "allow_retry_once": True,
                    "stop_on_first_failure": False,
                    "quarantine_file": "tests/e2e/quarantine.txt",
                }
            })

            assert response.status_code == 400
        finally:
            web._orchestrator = None

    def test_post_settings_reverts_on_save_failure(self):
        """POST /api/settings reverts in-memory changes if save fails."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        original_value = mock_orch.config.max_concurrent_sessions

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock(side_effect=IOError("Disk full"))

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 15,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 500
                assert "Disk full" in response.json()["error"]

                # Verify config was reverted
                assert mock_orch.config.max_concurrent_sessions == original_value
            finally:
                web._orchestrator = None

    def test_settings_page_renders(self):
        """GET /settings renders the settings page with schema-driven fields."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/settings")

            assert response.status_code == 200
            html = response.text
            assert "Settings" in html
            assert "Concurrency" in html
            assert "E2E Runner" in html
            assert "Filtering" in html
            assert "Review" in html
            assert "Advanced" in html
        finally:
            web._orchestrator = None

    def test_settings_page_renders_schema_fields(self):
        """GET /settings renders form fields with data-tab/data-field attributes."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/settings")

            html = response.text
            # Check schema-driven data attributes are present
            assert 'data-tab="concurrency"' in html
            assert 'data-field="max_concurrent_sessions"' in html
            assert 'data-type="integer"' in html
            assert 'data-type="boolean"' in html
            # Check that current values are rendered
            assert f'value="{mock_orch.config.max_concurrent_sessions}"' in html
        finally:
            web._orchestrator = None

    def test_settings_page_embeds_schema_json(self):
        """GET /settings embeds schema JSON for client-side validation."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/settings")

            html = response.text
            assert "SCHEMA_TABS" in html
            assert "SCHEMA_FIELDS" in html
            assert "const SCHEMA_TABS = [" in html
            assert "const SCHEMA_FIELDS = {" in html
        finally:
            web._orchestrator = None

    def test_settings_page_uses_tojson_for_inline_schema_bootstrap(self):
        """GET /settings must neutralize script-closing text in inline schema JSON."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.infra.settings_schema import get_settings_json_schema

        payload = '</script><script>window.__xss_probe__=1</script>'
        custom_schema = get_settings_json_schema()
        custom_schema["advanced"]["properties"]["dangerous"] = {
            "type": "string",
            "description": payload,
        }
        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            with patch(
                "issue_orchestrator.entrypoints.web_settings_routes.get_settings_json_schema",
                return_value=custom_schema,
            ):
                client = TestClient(app)
                response = client.get("/settings")

            html = response.text
            assert response.status_code == 200
            assert payload not in html
            assert r"\u003c/script\u003e\u003cscript\u003ewindow.__xss_probe__=1\u003c/script\u003e" in html
        finally:
            web._orchestrator = None

    def test_settings_page_renders_without_orchestrator(self):
        """GET /settings renders with default config when no orchestrator."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None
        client = TestClient(app)
        response = client.get("/settings")

        assert response.status_code == 200
        assert "Settings" in response.text

    def test_settings_page_propagates_embedded_context_via_shared_helper(self):
        """Regression: the Settings page must route Cancel/back through the
        shared embeddedNav helper so the Dashboard round-trip preserves both
        embedded=1 and theme. The CC iframe is loaded with both params, and
        because CC and dashboard can live on different ports (different
        origins), localStorage is not shared — the URL theme is load-bearing.
        """
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None
        client = TestClient(app)
        response = client.get("/settings")

        assert response.status_code == 200
        html = response.text
        # Shared helper must be loaded before the inline script.
        assert '<script src="/static/js/theme_resolution.js"></script>' in html
        assert '<script src="/static/js/embedded_nav.js"></script>' in html
        assert html.index('/static/js/theme_resolution.js') < html.index(
            '/static/js/embedded_nav.js'
        )
        # Back link and Cancel must delegate to the helper.
        assert 'id="backToDashboardLink"' in html
        assert 'id="cancelSettingsBtn"' in html
        assert 'onclick="cancelSettings()"' in html
        assert "window.embeddedNav.buildHref('/', window.location.search)" in html
        assert "const SCHEMA_TABS = [" in html
        assert "const SCHEMA_FIELDS = {" in html
        assert "const SCHEMA_TABS = [&#34;" not in html
        assert "const SCHEMA_FIELDS = {&#34;" not in html
        # Old ad-hoc helpers and literal URLs must be gone.
        assert "settingsIsEmbedded" not in html
        assert "'/?embedded=1'" not in html
        assert "onclick=\"window.location.href='/'\"" not in html

    def test_get_settings_filtering_with_milestones(self):
        """GET /api/settings returns milestones as comma-separated string."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.milestones = ["M1", "M2"]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()

            # Schema returns milestones as comma-separated string
            assert data["filtering"]["milestones"] == "M1, M2"
        finally:
            web._orchestrator = None

    def test_get_settings_filtering_with_singular_milestone(self):
        """GET /api/settings handles singular milestone field via get_milestones()."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.milestone = "v1.0"
        mock_orch.config.filtering.milestones = []

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()

            # get_milestones() returns ["v1.0"], schema joins with comma
            assert data["filtering"]["milestones"] == "v1.0"
        finally:
            web._orchestrator = None

    def test_post_settings_milestones_comma_separated(self):
        """POST /api/settings handles comma-separated milestones string."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.milestone = "old-milestone"

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "filtering": {
                        "label": None,
                        "milestones": "M1, M2",
                        "exclude_labels": "",
                        "fetch_limit": 100,
                        "max_to_start": 0,
                    }
                })

                assert response.status_code == 200

                # Comma-separated string should be split into list
                assert mock_orch.config.filtering.milestones == ["M1", "M2"]
            finally:
                web._orchestrator = None

    def test_post_settings_empty_milestones(self):
        """POST /api/settings handles empty milestones string."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "filtering": {
                        "label": None,
                        "milestones": "",
                        "exclude_labels": "",
                        "fetch_limit": 100,
                        "max_to_start": 0,
                    }
                })

                assert response.status_code == 200
                assert mock_orch.config.filtering.milestones == []
            finally:
                web._orchestrator = None

    def test_post_settings_restart_required(self):
        """POST /api/settings signals restart_required when port changes."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "advanced": {
                        "session_no_output_seconds": 120,
                        "stale_escalation_ticks": 0,
                        "web_port": 9090,
                        "control_api_port": 19080,
                        "worktree_base": str(mock_orch.config.worktree_base),
                        "worktree_branch_on_recreate": "delete",
                    }
                })

                assert response.status_code == 200
                data = response.json()
                assert data["restart_required"] is True
                assert mock_orch.config.web_port == 9090
            finally:
                web._orchestrator = None

    def test_post_settings_partial_tabs_preserve_others(self):
        """POST /api/settings with partial tabs preserves unchanged tabs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        original_e2e_enabled = mock_orch.config.e2e.enabled

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                # Only send concurrency tab
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 10,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                assert mock_orch.config.max_concurrent_sessions == 10
                # E2E settings should be unchanged
                assert mock_orch.config.e2e.enabled == original_e2e_enabled
            finally:
                web._orchestrator = None

    def test_post_settings_partial_tabs_preserve_path_values_for_doctor(self, tmp_path):
        """Partial settings saves keep Path-typed config fields valid before doctor runs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo_root = (tmp_path / "repo").resolve()
        mock_orch.config.worktree_base = (tmp_path / "worktrees").resolve()
        original_worktree_base = mock_orch.config.worktree_base

        def run_doctor_asserts_path(config, runner):
            assert config.worktree_base == original_worktree_base
            assert isinstance(config.worktree_base, Path)
            mock_result = MagicMock()
            mock_result.checks = []
            return mock_result

        with patch("issue_orchestrator.infra.doctor.run_doctor", side_effect=run_doctor_asserts_path):
            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 2,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                assert mock_orch.config.max_concurrent_sessions == 2
                assert mock_orch.config.worktree_base == original_worktree_base
                assert isinstance(mock_orch.config.worktree_base, Path)
            finally:
                web._orchestrator = None


class TestStaticFilesSecurity:
    """Tests for static file serving security."""

    def test_path_traversal_blocked_css(self):
        """Path traversal attempts in CSS route should return 404."""
        client = TestClient(app)
        # Attempt to traverse out of static directory
        response = client.get("/static/css/../../../templates/dashboard.html")
        assert response.status_code == 404

    def test_path_traversal_blocked_js(self):
        """Path traversal attempts in JS route should return 404."""
        client = TestClient(app)
        # Attempt to traverse out of static directory
        response = client.get("/static/js/../../../entrypoints/web.py")
        assert response.status_code == 404

    def test_valid_css_file_served(self):
        """Valid CSS files should be served correctly."""
        client = TestClient(app)
        response = client.get("/static/css/ui_primitives.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")
        assert ".ui-btn" in response.text

    def test_valid_dashboard_css_chunk_served(self):
        """Dashboard CSS chunks should resolve through static serving."""
        client = TestClient(app)
        response = client.get("/static/css/dashboard/base.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")
        assert ":root {" in response.text

    def test_valid_js_file_served(self):
        """Valid JS files should be served correctly."""
        client = TestClient(app)
        response = client.get("/static/js/dashboard.js")
        assert response.status_code == 200
        assert "javascript" in response.headers.get("content-type", "")

    def test_valid_dashboard_js_chunk_served(self):
        """Dashboard JS chunks should resolve through static serving."""
        client = TestClient(app)
        response = client.get("/static/js/dashboard/kanban_columns.js")
        assert response.status_code == 200
        assert "javascript" in response.headers.get("content-type", "")
        assert "function renderCompactCards" in response.text

    def test_packaged_brand_logo_served(self):
        """Brand assets should resolve through package static serving."""
        client = TestClient(app)
        response = client.get("/static/brand/logo.svg")
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers.get("content-type", "")
        assert "<svg" in response.text

    def test_favicon_uses_packaged_logo(self):
        """Legacy favicon route should not depend on repo-root assets."""
        client = TestClient(app)
        response = client.get("/favicon.ico")
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers.get("content-type", "")
        assert "<svg" in response.text


class TestIssueAuditEndpoint:
    """Tests for explicit issue audit refresh endpoint."""

    def test_force_issue_audit_returns_failure_diagnosis(self):
        mock_orch = create_mock_orchestrator()
        mock_orch.get_failure_diagnosis.return_value = {
            "issue_number": 4057,
            "analysis_headline": "Timed out while exploring unrelated files",
            "suggestions": ["Narrow the task prompt"],
        }
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/issues/4057/audit")

        assert response.status_code == 200
        assert response.json()["issue_number"] == 4057
        assert response.json()["analysis_headline"] == "Timed out while exploring unrelated files"
        mock_orch.get_failure_diagnosis.assert_called_once_with(4057)

    def test_force_issue_audit_requires_orchestrator(self):
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post("/api/issues/4057/audit")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}
