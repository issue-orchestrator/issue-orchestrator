"""Unit tests for runtime E2E stdout/stderr capture artifacts."""

from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.infra.e2e_runtime_output import (
    read_runtime_captured_output,
    runtime_output_path,
    write_runtime_captured_output,
    write_pytest_reports_captured_output,
)


def test_runtime_captured_output_round_trips_by_nodeid(tmp_path: Path) -> None:
    nodeid = "tests/e2e/test_smoke.py::test_chatty"

    written = write_runtime_captured_output(
        tmp_path,
        12,
        nodeid,
        system_out=" captured stdout \n",
        system_err="captured stderr",
    )

    assert written is not None
    assert written.source_path == runtime_output_path(tmp_path, 12, nodeid)
    read = read_runtime_captured_output(tmp_path, 12, nodeid)
    assert read is not None
    assert read.nodeid == nodeid
    assert read.system_out == "captured stdout"
    assert read.system_err == "captured stderr"
    assert read.source_path == written.source_path


def test_runtime_captured_output_ignores_empty_channels(tmp_path: Path) -> None:
    captured = write_runtime_captured_output(
        tmp_path,
        12,
        "tests/e2e/test_smoke.py::test_quiet",
        system_out="   ",
        system_err=None,
    )

    assert captured is None


def test_pytest_report_capture_combines_phase_output(tmp_path: Path) -> None:
    nodeid = "tests/e2e/test_smoke.py::test_chatty"

    written = write_pytest_reports_captured_output(
        tmp_path,
        12,
        nodeid,
        [
            SimpleNamespace(capstdout="setup stdout", capstderr="", caplog=""),
            SimpleNamespace(capstdout="call stdout", capstderr="call stderr", caplog="call log"),
            SimpleNamespace(capstdout="", capstderr="teardown stderr", caplog=""),
        ],
    )

    assert written is not None
    assert written.system_out == "setup stdout\ncall stdout\ncall log"
    assert written.system_err == "call stderr\nteardown stderr"
