"""The suite must not write to the host's real Control Center log."""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.entrypoints import control_center
from issue_orchestrator.infra import logging_config
from tests.control_center_log_isolation import control_center_log_isolated


def _file_logs_on_root_logger() -> list[str]:
    return [
        handler.baseFilename
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
    ]


def test_driving_main_writes_no_log_under_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` is how the real log was reached, so drive ``main()`` itself."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    args = Namespace(
        port=19080,
        host="127.0.0.1",
        debug=False,
        debug_http=False,
        no_browser=True,
        no_tray=True,
        dev_no_auth=True,
    )

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=args),
        patch("issue_orchestrator.entrypoints.control_center.uvicorn.run"),
        patch("issue_orchestrator.entrypoints.control_center.write_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.clear_dashboard_pid"),
        patch("issue_orchestrator.entrypoints.control_center.logging.basicConfig"),
        patch(
            "issue_orchestrator.infra.repo_registry.cleanup_stale_repos", return_value=0
        ),
        patch(
            "issue_orchestrator.entrypoints.control_center.threading.Thread"
        ) as thread_cls,
    ):
        thread_cls.return_value = MagicMock(start=MagicMock())
        assert control_center.main() == 0

    # main() did attach its file log, so this test exercised the real path...
    assert str(tmp_path / "control-center.log") in _file_logs_on_root_logger()
    # ...and neither the handler nor the file went anywhere near the home dir.
    assert not (home / ".issue-orchestrator" / "logs").exists()
    assert not any(path.startswith(str(home)) for path in _file_logs_on_root_logger())


def test_the_file_log_does_not_outlive_the_test_that_attached_it(
    tmp_path: Path,
) -> None:
    """A surviving handler would catch every later test in that xdist worker."""
    log_file = tmp_path / "inner" / "control-center.log"
    log_file.parent.mkdir()
    path_function = logging_config.get_control_center_log_path

    with control_center_log_isolated(log_file):
        logging_config.add_rotating_file_handler(
            logging_config.get_control_center_log_path(), level=logging.INFO
        )
        attached = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, logging.FileHandler)
            and handler.baseFilename == str(log_file)
        ]
        assert len(attached) == 1

    assert str(log_file) not in _file_logs_on_root_logger()
    assert attached[0].stream is None  # closed, so no file descriptor leaks either
    assert logging_config.get_control_center_log_path is path_function


def test_a_test_s_other_file_handlers_are_left_alone(tmp_path: Path) -> None:
    """The isolation owns one log, not whatever else a test chose to attach."""
    other = logging.FileHandler(tmp_path / "other.log")
    root = logging.getLogger()
    root.addHandler(other)
    try:
        with control_center_log_isolated(tmp_path / "control-center.log"):
            pass
        assert other in root.handlers
    finally:
        root.removeHandler(other)
        other.close()
