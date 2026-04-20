"""Unit tests for the system tray icon module."""

# ruff: noqa: SLF001

from __future__ import annotations

import os
from pathlib import Path
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

# pystray import fails on headless Linux (Xlib needs a display)
try:
    import pystray as _pystray  # noqa: F401
    _pystray_available = True
except Exception:
    _pystray_available = False

try:
    import PIL as _pil  # noqa: F401
    _pil_available = True
except Exception:
    _pil_available = False

_skip_no_pystray = pytest.mark.skipif(
    not _pystray_available,
    reason="pystray requires a display (unavailable on headless CI)",
)
_skip_no_pil = pytest.mark.skipif(
    not _pil_available,
    reason="Pillow is unavailable",
)


def test_default_brand_assets_are_packaged() -> None:
    """Runtime brand assets should be package-owned files."""
    from issue_orchestrator.entrypoints.brand_assets import (
        LOGO_SVG_BYTES,
        LOGO_SVG_PATH,
        TRAY_ICON_PNG_PATH,
    )

    assert LOGO_SVG_BYTES.startswith(b"<svg")
    assert LOGO_SVG_PATH.is_file()
    assert TRAY_ICON_PNG_PATH.is_file()


@_skip_no_pil
class TestLoadIcon:
    """Tests for _load_icon()."""

    def test_loads_png_from_assets(self, tmp_path: Path) -> None:
        """When tray-icon.png exists, load it."""
        from issue_orchestrator.entrypoints import tray

        png_path = tmp_path / "tray-icon.png"
        png_path.write_bytes(b"fake-png")

        with (
            patch.object(tray, "TRAY_ICON_PNG_PATH", png_path),
            patch("PIL.Image.open") as mock_open,
        ):
            result = tray._load_icon()

        mock_open.assert_called_once_with(png_path)
        assert result == mock_open.return_value

    def test_generates_fallback_when_png_missing(self, tmp_path: Path) -> None:
        """When tray-icon.png is absent, generate a fallback circle."""
        from issue_orchestrator.entrypoints import tray
        png_path = tmp_path / "tray-icon.png"

        with (
            patch.object(tray, "TRAY_ICON_PNG_PATH", png_path),
            patch("PIL.Image.new") as mock_new,
            patch("PIL.ImageDraw.Draw"),
        ):
            result = tray._load_icon()

        mock_new.assert_called_once_with("RGBA", (64, 64), (0, 0, 0, 0))
        assert result == mock_new.return_value


@_skip_no_pystray
class TestBuildMenu:
    """Tests for _build_menu()."""

    def test_menu_includes_engine_status(self) -> None:
        """Menu items include engine names and states."""
        from issue_orchestrator.entrypoints import tray

        engines = [("my-app", "running"), ("api-service", "stopped")]

        with patch("pystray.Menu") as mock_menu, patch("pystray.MenuItem") as mock_item:
            mock_menu.SEPARATOR = "---"
            tray._build_menu("http://localhost:19080/", lambda: engines)

            # pystray.Menu is called with a callable
            mock_menu.assert_called_once()
            menu_items_fn = mock_menu.call_args[0][0]

            # Invoke the callable to get menu items
            items = menu_items_fn()

        # 1 running engine item + separator + "Open Control Center"
        assert len(items) == 3

        # Check engine items have correct labels
        first_label = mock_item.call_args_list[0][0][0]
        assert "my-app" in first_label
        assert "running" in first_label
        assert "\u25cf" in first_label  # filled circle for running
        assert all("api-service" not in call[0][0] for call in mock_item.call_args_list)

    def test_menu_includes_open_control_center(self) -> None:
        """Menu always includes 'Open Control Center' item."""
        from issue_orchestrator.entrypoints import tray

        with patch("pystray.Menu") as mock_menu, patch("pystray.MenuItem") as mock_item:
            mock_menu.SEPARATOR = "---"
            tray._build_menu("http://localhost:19080/", lambda: [])

            menu_items_fn = mock_menu.call_args[0][0]
            items = menu_items_fn()

        # No engines -> "No running engines" + separator + "Open Control Center"
        assert len(items) == 3
        assert mock_item.call_args_list[0][0][0] == "No running engines"
        assert mock_item.call_args_list[1][0][0] == "Open Control Center"

    def test_separator_present_when_no_engines(self) -> None:
        """Separator appears between empty-state line and Open Control Center."""
        from issue_orchestrator.entrypoints import tray

        with patch("pystray.Menu") as mock_menu, patch("pystray.MenuItem"):
            mock_menu.SEPARATOR = "---"
            tray._build_menu("http://localhost:19080/", lambda: [])

            menu_items_fn = mock_menu.call_args[0][0]
            items = menu_items_fn()

        assert "---" in items

    def test_engine_status_fn_exception_handled(self) -> None:
        """If engine_status_fn raises, menu still renders with Open CC."""
        from issue_orchestrator.entrypoints import tray

        def _failing_fn() -> list[tuple[str, str]]:
            raise RuntimeError("status fetch failed")

        with patch("pystray.Menu") as mock_menu, patch("pystray.MenuItem"):
            mock_menu.SEPARATOR = "---"
            tray._build_menu("http://localhost:19080/", _failing_fn)

            menu_items_fn = mock_menu.call_args[0][0]
            items = menu_items_fn()

        # Should still show empty state and Open Control Center
        assert len(items) == 3


@_skip_no_pystray
class TestStartTray:
    """Tests for start_tray()."""

    def test_creates_icon_and_starts_detached_loop(self) -> None:
        """start_tray creates an Icon and starts it via run_detached()."""
        from issue_orchestrator.entrypoints import tray

        mock_icon = MagicMock()
        mock_image = MagicMock()

        with (
            patch("pystray.Icon", return_value=mock_icon) as mock_icon_cls,
            patch("pystray.Menu") as mock_menu,
            patch.object(tray, "_load_icon", return_value=mock_image),
        ):
            mock_menu.SEPARATOR = "---"

            result = tray.start_tray(
                "http://localhost:19080/",
                lambda: [],
            )

        assert result is mock_icon
        mock_icon_cls.assert_called_once_with(
            "issue-orchestrator",
            mock_image,
            "Issue Orchestrator",
            mock_menu.return_value,
        )
        mock_icon.run_detached.assert_called_once_with()

    def test_uses_provided_icon_image(self) -> None:
        """start_tray uses a custom image when provided."""
        from issue_orchestrator.entrypoints import tray

        mock_icon = MagicMock()
        custom_image = MagicMock()

        with (
            patch("pystray.Icon", return_value=mock_icon) as mock_icon_cls,
            patch("pystray.Menu") as mock_menu,
            patch.object(tray, "_load_icon") as mock_load,
        ):
            mock_menu.SEPARATOR = "---"
            tray.start_tray(
                "http://localhost:19080/",
                lambda: [],
                icon_image=custom_image,
            )

        # Should use custom_image, not call _load_icon
        mock_load.assert_not_called()
        assert mock_icon_cls.call_args[0][1] is custom_image


@_skip_no_pystray
class TestOpenControlCenter:
    """Test the 'Open Control Center' menu action."""

    def test_open_control_center_opens_browser(self) -> None:
        """Clicking 'Open Control Center' calls webbrowser.open."""
        from issue_orchestrator.entrypoints import tray

        url = "http://localhost:19080/"

        with (
            patch("pystray.Menu") as mock_menu,
            patch("pystray.MenuItem") as mock_item,
        ):
            mock_menu.SEPARATOR = "---"
            tray._build_menu(url, lambda: [])

            menu_items_fn = mock_menu.call_args[0][0]
            menu_items_fn()

        # Get the callback from the MenuItem call (Open Control Center)
        callback = mock_item.call_args_list[-1][0][1]

        with patch.object(tray.webbrowser, "open") as mock_open:
            callback()
            mock_open.assert_called_once_with(url)


class TestTrayControlCenterStatus:
    """Tests for Control Center status fetch used by tray helper mode."""

    def test_engine_status_returns_pairs_from_control_api(self) -> None:
        """Engine status parser extracts (name, state) pairs."""
        from issue_orchestrator.entrypoints import tray

        payload = (
            b'{"repos":[{"name":"repo-a","status":{"state":"stopped","running_count":1}},'
            b'{"name":"repo-b","status":{"state":"stopped"}}]}'
        )
        response = MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=response):
            result = tray._engine_status_from_control_center("http://localhost:19080/")  # noqa: SLF001

        assert result == [("repo-a", "running"), ("repo-b", "stopped")]

    def test_engine_status_returns_empty_on_transport_error(self) -> None:
        """Transport errors are tolerated and reported as empty status."""
        from issue_orchestrator.entrypoints import tray

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            result = tray._engine_status_from_control_center("http://localhost:19080/")  # noqa: SLF001

        assert result == []

    def test_dashboard_url_validator_rejects_relative_url(self) -> None:
        """Tray helper only accepts absolute HTTP(S) dashboard URLs."""
        from issue_orchestrator.entrypoints import tray

        with pytest.raises(Exception):
            tray._dashboard_url("/relative/path")  # noqa: SLF001


class TestTrayOwnerProcess:
    """Tests for tray owner process lifecycle helpers."""

    def test_process_exists_true_when_kill_succeeds(self) -> None:
        """PID is considered alive when os.kill(pid, 0) succeeds."""
        from issue_orchestrator.entrypoints import tray

        with patch("os.kill") as kill_mock:
            assert tray._process_exists(1234) is True  # noqa: SLF001
            kill_mock.assert_called_once_with(1234, 0)

    def test_process_exists_false_when_missing(self) -> None:
        """Missing PID is reported as not alive."""
        from issue_orchestrator.entrypoints import tray

        with patch("os.kill", side_effect=ProcessLookupError):
            assert tray._process_exists(1234) is False  # noqa: SLF001
