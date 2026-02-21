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
            "issue_orchestrator.entrypoints.control_center.subprocess.check_output",
            return_value=(
                "1234 python -m issue_orchestrator.entrypoints.tray --dashboard-url http://127.0.0.1:19080/\n"
                "5678 python -m issue_orchestrator.entrypoints.tray --dashboard-url http://127.0.0.1:29080/\n"
            ),
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
            "issue_orchestrator.entrypoints.control_center.subprocess.Popen",
            return_value=process,
        ) as popen_mock,
    ):
        result = control_center._start_tray_icon("http://localhost:19080/")  # noqa: SLF001

    popen_mock.assert_called_once()
    args = popen_mock.call_args.args[0]
    assert "--owner-pid" in args
    assert result is not None
    result.stop()
    process.terminate.assert_called_once_with()


def test_start_tray_icon_returns_none_when_helper_exits_immediately() -> None:
    """macOS helper startup failure should degrade to no tray handle."""
    process = MagicMock(pid=1234, returncode=1)
    process.poll.return_value = 1

    with (
        patch("issue_orchestrator.entrypoints.control_center.sys.platform", "darwin"),
        patch(
            "issue_orchestrator.entrypoints.control_center.subprocess.Popen",
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
            "issue_orchestrator.entrypoints.control_api._build_repos_status",
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
            "issue_orchestrator.entrypoints.control_api._build_repos_status",
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
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "issue_orchestrator"
        / "templates"
        / "control_center.html"
    ).read_text(encoding="utf-8")

    assert "const pendingRepoStarts = new Set();" in template
    assert "if (pendingRepoStarts.has(path))" in template
    assert "pendingRepoStarts.add(path);" in template
    assert "pendingRepoStarts.delete(path);" in template
    assert "Starting..." in template
    assert 'id="repoStopModal"' in template
    assert 'id="confirmRepoStop"' in template
    assert 'id="repoStopModeGraceful"' in template
    assert 'id="repoStopModeForce"' in template
    assert 'id="shutdownModeGraceful"' in template
    assert 'id="shutdownModeForce"' in template
    assert "Mode: immediate force" in template
    assert "Mode: graceful (~${formatGracefulTimeout(op.graceful_timeout_seconds || 120)}), then force if needed" in template
    assert "State: control center unavailable" in template
    assert "Control Center is closing..." in template
    assert "showControlCenterClosedFallback" in template
    assert "attemptControlCenterTabClose" in template
    assert 'data-action="stop-force"' not in template
