"""Unit tests for control center entrypoint helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from issue_orchestrator.entrypoints import control_center


def test_start_tray_icon_returns_icon_when_available() -> None:
    """_start_tray_icon returns the tray icon when startup succeeds."""
    mock_icon = MagicMock()
    captured: dict[str, object] = {}

    def _fake_start_tray(*, dashboard_url: str, engine_status_fn):
        captured["dashboard_url"] = dashboard_url
        captured["engine_status"] = engine_status_fn()
        return mock_icon

    with (
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
