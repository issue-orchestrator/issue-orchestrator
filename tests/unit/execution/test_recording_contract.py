"""Unit tests for ``recording_contract`` — terminal-recording JSONL validation.

Moved out of ``test_persistent_round_runner.py`` alongside the extraction of
``recording_event_count`` into its own module: the contract operates purely on
recording files and has no PTY/session coupling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.execution.recording_contract import (
    CorruptRecordingError,
    recording_event_count,
)


class TestRecordingEventCount:
    def test_default_raises_for_missing_recording(self, tmp_path: Path) -> None:
        """Per session-replay contract: a missing recording when one is
        expected is a caller bug, not a zero-event signal that would
        produce wrong-but-plausible chapter offsets."""
        with pytest.raises(FileNotFoundError):
            recording_event_count(tmp_path / "absent.jsonl")

    def test_explicit_opt_out_returns_zero_for_missing(self, tmp_path: Path) -> None:
        """Bootstrap and test paths that genuinely have no recording yet
        opt out of the existence check."""
        assert recording_event_count(
            tmp_path / "absent.jsonl",
            require_recording=False,
        ) == 0

    def test_counts_valid_recording_events_skipping_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"resize","offset_ms":0,"rows":40,"cols":120}\n\n'
            '{"schema_version":1,"event_type":"output","offset_ms":12,"data_b64":"aGk="}\n  \n'
            '{"schema_version":1,"event_type":"output","offset_ms":99,"data_b64":"YnllCg=="}\n',
            encoding="utf-8",
        )
        assert recording_event_count(path) == 3

    def test_raises_on_malformed_json_line(self, tmp_path: Path) -> None:
        """A corrupt recording must surface loudly — the offset feeds into
        chapters.json and the session viewer scrubs to it. A wrong-but-
        plausible count is worse than a loud failure."""
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","offset_ms":0,"data_b64":"aGk="}\n'
            "not-json\n",
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="Malformed JSON"):
            recording_event_count(path)

    def test_raises_when_event_is_not_an_object(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text('"just a string"\n', encoding="utf-8")
        with pytest.raises(CorruptRecordingError, match="not a JSON object"):
            recording_event_count(path)

    def test_raises_when_event_type_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"offset_ms":0,"data_b64":"aGk="}\n', encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="missing event_type"):
            recording_event_count(path)

    def test_raises_when_schema_version_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"event_type":"output","offset_ms":0,"data_b64":"aGk="}\n', encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="schema_version"):
            recording_event_count(path)

    def test_raises_when_offset_ms_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","data_b64":"aGk="}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="offset_ms"):
            recording_event_count(path)

    def test_raises_when_output_event_lacks_data_b64(self, tmp_path: Path) -> None:
        """Replay can't render an output event without payload bytes — that
        line must not advance the chapter offset."""
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","offset_ms":0}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="data_b64"):
            recording_event_count(path)

    def test_raises_when_resize_event_lacks_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"resize","offset_ms":0,"cols":120}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="missing integer rows"):
            recording_event_count(path)

    def test_raises_when_resize_event_lacks_cols(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"resize","offset_ms":0,"rows":40}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="missing integer cols"):
            recording_event_count(path)

    def test_raises_when_output_data_b64_is_not_valid_base64(self, tmp_path: Path) -> None:
        """An output event whose ``data_b64`` is non-empty but not actually
        base64 will crash the browser replay decoder at scrub time, so it
        must not advance the chapter offset."""
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","offset_ms":0,'
            '"data_b64":"@@@@"}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="not valid base64"):
            recording_event_count(path)

    def test_raises_on_unsupported_event_type(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"junk","offset_ms":0}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="unsupported event_type"):
            recording_event_count(path)
