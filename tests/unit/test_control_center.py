"""Unit tests for control center entrypoint helpers."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from issue_orchestrator.entrypoints import control_center


def test_cleanup_stale_tray_helpers_kills_matching_url() -> None:
    """Cleanup should terminate tray helpers for the same dashboard URL."""
    with (
        patch(
            "issue_orchestrator.entrypoints.control_center.list_processes_matching",
            return_value=[
                (1234, "python -m issue_orchestrator.entrypoints.tray --dashboard-url http://127.0.0.1:19080/"),
                (5678, "python -m issue_orchestrator.entrypoints.tray --dashboard-url http://127.0.0.1:29080/"),
            ],
        ),
        patch("issue_orchestrator.entrypoints.control_center.os.getpid", return_value=9999),
        patch("issue_orchestrator.entrypoints.control_center.os.kill") as kill_mock,
    ):
        control_center._cleanup_stale_tray_helpers("http://127.0.0.1:19080/")  # noqa: SLF001

    kill_mock.assert_called_once()
    assert kill_mock.call_args.args[0] == 1234


def test_start_tray_icon_starts_helper_process_on_macos() -> None:
    """macOS uses tray helper subprocess to keep Cocoa loop isolated."""
    process = MagicMock(pid=1234)
    process.poll.return_value = None

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "darwin"),
        patch("issue_orchestrator.entrypoints.control_center._cleanup_stale_tray_helpers"),
        patch(
            "issue_orchestrator.entrypoints.control_center.spawn_tray_helper",
            return_value=process,
        ) as spawn_mock,
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs["dashboard_url"] == "http://localhost:19080/"
    assert result is not None
    result.stop()
    process.stop.assert_called_once_with(graceful_timeout_seconds=2)


def test_start_tray_icon_returns_none_when_helper_exits_immediately() -> None:
    """macOS helper startup failure should degrade to no tray handle."""
    process = MagicMock(pid=1234, returncode=1)
    process.poll.return_value = 1

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "darwin"),
        patch(
            "issue_orchestrator.entrypoints.control_center.spawn_tray_helper",
            return_value=process,
        ),
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    assert result is None


def test_start_tray_icon_returns_icon_when_available() -> None:
    """_start_tray_icon returns the tray icon when startup succeeds."""
    mock_icon = MagicMock()
    captured: dict[str, object] = {}

    def _fake_start_tray(*, dashboard_url: str, engine_status_fn):
        captured["dashboard_url"] = dashboard_url
        captured["engine_status"] = engine_status_fn()
        return mock_icon

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "linux"),
        patch(
            "issue_orchestrator.execution.control_center_repo_status.build_repos_status",
            return_value=[{"name": "repo-a", "status": {"state": "running"}}],
        ),
        patch(
            "issue_orchestrator.entrypoints.tray.start_tray",
            side_effect=_fake_start_tray,
        ),
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    assert result is mock_icon
    assert captured["dashboard_url"] == "http://localhost:19080/"
    assert captured["engine_status"] == [("repo-a", "running")]


def test_start_tray_icon_returns_none_when_startup_fails() -> None:
    """_start_tray_icon degrades gracefully when tray startup fails."""
    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "linux"),
        patch(
            "issue_orchestrator.entrypoints.tray.start_tray",
            side_effect=RuntimeError("no tray backend"),
        ),
        patch(
            "issue_orchestrator.execution.control_center_repo_status.build_repos_status",
            return_value=[],
        ),
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    assert result is None


def _make_main_args(*, debug_http: bool) -> Namespace:
    return Namespace(
        port=19080,
        host="127.0.0.1",
        debug=False,
        debug_http=debug_http,
        no_browser=True,
        no_tray=True,
    )


def test_main_disables_uvicorn_access_log_by_default() -> None:
    """main() starts uvicorn with access logs disabled by default."""
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_make_main_args(debug_http=False)),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run") as run_mock,
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        result = control_center.main()

    assert result == 0
    assert run_mock.call_args.kwargs["access_log"] is False


def test_main_enables_uvicorn_access_log_with_debug_http_flag() -> None:
    """main() enables uvicorn access logs when --debug-http is set."""
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_make_main_args(debug_http=True)),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run") as run_mock,
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch("issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0),
        patch("issue_orchestrator.entrypoints.control_center.threading.Thread") as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        result = control_center.main()

    assert result == 0
    assert run_mock.call_args.kwargs["access_log"] is True


def test_start_buttons_are_disabled_while_start_is_pending() -> None:
    """Control Center tracks in-flight starts and disables start buttons."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
    template = (source_root / "templates" / "control_center.html").read_text(encoding="utf-8")
    script = (source_root / "static" / "js" / "control_center.js").read_text(encoding="utf-8")

    assert '<script src="/static/js/control_center.js"></script>' in template
    assert "const pendingRepoStarts = new Set();" in script
    assert "const REPO_START_POLL_INTERVAL_MS = 250;" in script
    assert "const REPO_START_TIMEOUT_MS = 15000;" in script
    assert "function getSelectedRepoConfig(repo, preferredConfig = null)" in script
    assert "function requiresExplicitRepoConfigSelection(repo, selectedConfig)" in script
    assert "if (pendingRepoStarts.has(path))" in script
    assert "pendingRepoStarts.add(path);" in script
    assert "pendingRepoStarts.delete(path);" in script
    assert "async function waitForRepoToBeReady(path)" in script
    assert "await waitForRepoToBeReady(path);" in script
    assert "config = getValidRepoConfig(repo, config);" in script
    assert "throw new Error('Select a valid config before starting this repository engine');" in script
    assert (
        "function showDoctorResultsModal(title, data, prefixMessage = null, "
        "prefixClass = 'info', context = {})" in script
    )
    assert 'id="repairGuardrailsBtn"' in template
    assert "function hasRepairableRepoGuardrails(data)" in script
    assert "function repairGuardrails()" in script
    assert "const confirmed = window.confirm(" in script
    assert "This will overwrite issue-orchestrator managed hook files if they drifted." in script
    assert "if (!confirmed) {" in script
    assert "/control/orchestrator/guardrails/repair" in script
    assert "Repair Guardrails" in template
    assert "{ repoRoot: path, configName: config }" in script
    assert "Guardrails repaired (${fileCount} file${fileCount === 1 ? '' : 's'} written)" in script
    assert "if (!data) {" in script
    assert "if (error.error === 'doctor_failed' && error.doctor)" in script
    assert "Start Blocked — ${repoLabel}" in script
    assert "See Doctor Results." in script
    assert "null," in script
    assert "Choose config..." in script
    assert "Saved config is unavailable:" in script
    assert "Select a valid config before starting this repository engine." in script
    assert "Starting..." in script
    assert "Starting repository engine..." in script
    assert "Waiting for engine to become ready." in script
    assert 'id="repoStopModal"' in template
    assert 'id="confirmRepoStop"' in template
    assert 'id="repoStopModeGraceful"' in template
    assert 'id="repoStopModeForce"' in template
    assert 'id="shutdownModeGraceful"' in template
    assert 'id="shutdownModeForce"' in template
    assert "Mode: immediate force" in script
    assert "Mode: graceful (~${formatGracefulTimeout(op.graceful_timeout_seconds || 120)}), then force if needed" in script
    assert "State: control center unavailable" in script
    assert "Control Center is closing..." in script
    assert "showControlCenterClosedFallback" in script
    assert "attemptControlCenterTabClose" in script
    assert 'data-action="stop-force"' not in template
    assert 'data-action="stop-force"' not in script
    assert "getRepoDashboardUrl(repo, { embedded: true, theme: iframeTheme })" in script
    assert "buildDashboardUrlFromBase(repo.dashboard_url, options)" in script
    assert "Boolean(repo.dashboard_url)" in script
    assert "http://127.0.0.1:${port}" not in template


def test_control_center_uses_packaged_brand_assets() -> None:
    """Header logo and favicon should be package-static assets."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
    template = (source_root / "templates" / "control_center.html").read_text(encoding="utf-8")

    assert '<link rel="icon" type="image/svg+xml" href="/static/brand/logo.svg">' in template
    assert '<img class="sidebar-logo-icon" src="/static/brand/logo.svg"' in template
    assert "/favicon.ico" not in template
