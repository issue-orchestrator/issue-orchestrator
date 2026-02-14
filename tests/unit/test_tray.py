"""Unit tests for the system tray icon module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestLoadIcon:
    """Tests for _load_icon()."""

    def test_loads_png_from_assets(self, tmp_path: Path) -> None:
        """When tray-icon.png exists, load it."""
        from issue_orchestrator.entrypoints import tray

        png_path = tmp_path / "tray-icon.png"
        png_path.write_bytes(b"fake-png")

        with (
            patch.object(tray, "_ASSETS_DIR", tmp_path),
            patch("PIL.Image.open") as mock_open,
        ):
            result = tray._load_icon()

        mock_open.assert_called_once_with(png_path)
        assert result == mock_open.return_value

    def test_generates_fallback_when_png_missing(self, tmp_path: Path) -> None:
        """When tray-icon.png is absent, generate a fallback circle."""
        from issue_orchestrator.entrypoints import tray

        with (
            patch.object(tray, "_ASSETS_DIR", tmp_path),
            patch("PIL.Image.new") as mock_new,
            patch("PIL.ImageDraw.Draw"),
        ):
            result = tray._load_icon()

        mock_new.assert_called_once_with("RGBA", (64, 64), (0, 0, 0, 0))
        assert result == mock_new.return_value


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

        # 2 engine items + separator + "Open Control Center"
        assert len(items) == 4

        # Check engine items have correct labels
        first_label = mock_item.call_args_list[0][0][0]
        assert "my-app" in first_label
        assert "running" in first_label
        assert "\u25cf" in first_label  # filled circle for running

        second_label = mock_item.call_args_list[1][0][0]
        assert "api-service" in second_label
        assert "stopped" in second_label
        assert "\u25cb" in second_label  # hollow circle for stopped

    def test_menu_includes_open_control_center(self) -> None:
        """Menu always includes 'Open Control Center' item."""
        from issue_orchestrator.entrypoints import tray

        with patch("pystray.Menu") as mock_menu, patch("pystray.MenuItem") as mock_item:
            mock_menu.SEPARATOR = "---"
            tray._build_menu("http://localhost:19080/", lambda: [])

            menu_items_fn = mock_menu.call_args[0][0]
            items = menu_items_fn()

        # No engines -> just "Open Control Center"
        assert len(items) == 1
        assert mock_item.call_args_list[0][0][0] == "Open Control Center"

    def test_no_separator_when_no_engines(self) -> None:
        """No separator when engine list is empty."""
        from issue_orchestrator.entrypoints import tray

        with patch("pystray.Menu") as mock_menu, patch("pystray.MenuItem"):
            mock_menu.SEPARATOR = "---"
            tray._build_menu("http://localhost:19080/", lambda: [])

            menu_items_fn = mock_menu.call_args[0][0]
            items = menu_items_fn()

        # Should not contain the separator
        assert "---" not in items

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

        # Should still have "Open Control Center"
        assert len(items) == 1


class TestStartTray:
    """Tests for start_tray()."""

    def test_creates_icon_and_starts_thread(self) -> None:
        """start_tray creates an Icon and starts it on a daemon thread."""
        from issue_orchestrator.entrypoints import tray

        mock_icon = MagicMock()
        mock_image = MagicMock()

        with (
            patch("pystray.Icon", return_value=mock_icon) as mock_icon_cls,
            patch("pystray.Menu") as mock_menu,
            patch.object(tray, "_load_icon", return_value=mock_image),
            patch("threading.Thread") as mock_thread_cls,
        ):
            mock_menu.SEPARATOR = "---"
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread

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
        mock_thread_cls.assert_called_once()
        call_kwargs = mock_thread_cls.call_args[1]
        assert call_kwargs["daemon"] is True
        assert callable(call_kwargs["target"])
        mock_thread.start.assert_called_once()

    def test_uses_provided_icon_image(self) -> None:
        """start_tray uses a custom image when provided."""
        from issue_orchestrator.entrypoints import tray

        mock_icon = MagicMock()
        custom_image = MagicMock()

        with (
            patch("pystray.Icon", return_value=mock_icon) as mock_icon_cls,
            patch("pystray.Menu") as mock_menu,
            patch.object(tray, "_load_icon") as mock_load,
            patch("threading.Thread"),
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
