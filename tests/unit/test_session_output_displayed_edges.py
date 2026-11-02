"""The session-output owner reads a run's log by the format it is stored in (#9587).

Callers that look for words a session displayed (the live auth check) get text
from ``SessionOutput.read_displayed_output_edges`` and never open the log. The
owner picks the run's log and decodes it by format: a terminal recording stores
every PTY chunk base64-encoded, so reading it raw found no banner and health
review #9586 burned its whole 90-minute timeout at "Login expired".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.terminal_recording import TerminalRecordingWriter

EDGE = 8192
BANNER = "Login expired · Please run /login"
TUI_BANNER = b"\x1b[38;2;150;108;30m\x1b[3GLogin\x1b[1Cexpired \xc2\xb7 Please run /login\x1b[39m"


@pytest.fixture
def session_output() -> FileSystemSessionOutput:
    return FileSystemSessionOutput()


@pytest.fixture
def run(tmp_path: Path, session_output: FileSystemSessionOutput) -> SessionRunAssets:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return session_output.start_run(
        worktree_path=worktree, session_name="issue-1", issue_number=1
    )


def _record(run: SessionRunAssets, *chunks: bytes) -> None:
    writer = TerminalRecordingWriter(
        run.terminal_recording.path, initial_rows=40, initial_cols=120
    )
    for chunk in chunks:
        writer.write_output(chunk)
    writer.close()


def test_a_terminal_recording_is_decoded_to_what_it_displayed(
    run: SessionRunAssets, session_output: FileSystemSessionOutput
) -> None:
    _record(run, TUI_BANNER)

    text = session_output.read_displayed_output_edges(run, edge_bytes=EDGE)

    assert BANNER in text
    assert "data_b64" not in text


def test_a_long_recording_is_read_at_both_ends(
    run: SessionRunAssets, session_output: FileSystemSessionOutput
) -> None:
    _record(run, b"HEAD-MARK ", *([b"ordinary output\r\n"] * 4000), TUI_BANNER)

    text = session_output.read_displayed_output_edges(run, edge_bytes=EDGE)

    assert text.startswith("HEAD-MARK")
    assert BANNER in text.split("\n")[-1]
    assert len(text.encode("utf-8")) <= 2 * EDGE + 1


@pytest.mark.parametrize("legacy_name", ["ui-session.log", "pane.log"])
def test_a_run_without_a_recording_falls_back_to_its_plain_log(
    legacy_name: str, run: SessionRunAssets, session_output: FileSystemSessionOutput
) -> None:
    (run.run_dir / legacy_name).write_bytes(b"starting\n" + TUI_BANNER + b"\n")

    text = session_output.read_displayed_output_edges(run, edge_bytes=EDGE)

    assert BANNER in text


def test_the_recording_is_read_in_preference_to_a_legacy_log(
    run: SessionRunAssets, session_output: FileSystemSessionOutput
) -> None:
    _record(run, b"RECORDED-OUTPUT")
    (run.run_dir / "ui-session.log").write_text("LEGACY-OUTPUT\n", encoding="utf-8")

    text = session_output.read_displayed_output_edges(run, edge_bytes=EDGE)

    assert "RECORDED-OUTPUT" in text
    assert "LEGACY-OUTPUT" not in text


def test_a_run_that_has_written_nothing_reads_as_no_text(
    run: SessionRunAssets, session_output: FileSystemSessionOutput
) -> None:
    """``start_run`` leaves an empty recording; there is nothing to scan yet."""
    assert session_output.read_displayed_output_edges(run, edge_bytes=EDGE) == ""
