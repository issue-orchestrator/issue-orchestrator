from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import pytest

from issue_orchestrator.infra.terminal_recording import (
    MAX_ROW_BYTES_PER_OUTPUT_BYTE,
    MAX_TERMINAL_COLS,
    MAX_TERMINAL_ROWS,
    MirroredTerminalRecordingWriter,
    TerminalOutputEdges,
    TerminalRecordingWriter,
    append_output_event,
    first_terminal_geometry,
    iter_terminal_recording,
    read_recording_output_edges,
)


def test_terminal_recording_writer_flushes_events_immediately(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"

    writer = TerminalRecordingWriter(recording_path)
    writer.write_output(b"live output\n")

    raw = recording_path.read_text(encoding="utf-8")
    writer.close()

    event = json.loads(raw.strip())
    payload = base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
    assert payload == "live output\n"


def test_terminal_recording_writer_records_initial_geometry_first(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    clock = IncrementingClock(now=100.0, step_seconds=0.002)

    writer = TerminalRecordingWriter(recording_path, initial_rows=40, initial_cols=120, clock=clock)
    writer.write_output(b"hello\n")
    writer.close()

    events = list(iter_terminal_recording(recording_path))
    assert events[0]["event_type"] == "resize"
    assert events[0]["rows"] == 40
    assert events[0]["cols"] == 120
    assert events[0]["offset_ms"] == 0
    assert first_terminal_geometry(recording_path) == (40, 120)
    assert events[1]["event_type"] == "output"


def test_append_output_event_uses_next_offset_after_existing_events(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 41,
                "data_b64": base64.b64encode(b"first").decode("ascii"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    append_output_event(recording_path, "second")

    events = list(iter_terminal_recording(recording_path))
    assert [event["offset_ms"] for event in events] == [41, 42]


def test_append_output_event_ignores_trailing_invalid_lines_when_finding_offset(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "output",
                        "offset_ms": 3,
                        "data_b64": base64.b64encode(b"ok").decode("ascii"),
                    }
                ),
                "{not-json",
                "",
            ]
        ),
        encoding="utf-8",
    )

    append_output_event(recording_path, "next")

    raw_lines = [line for line in recording_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert json.loads(raw_lines[0])["offset_ms"] == 3
    assert json.loads(raw_lines[-1])["offset_ms"] == 4


def test_first_terminal_geometry_returns_none_without_resize_events(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 0,
                "data_b64": base64.b64encode(b"plain").decode("ascii"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert first_terminal_geometry(recording_path) is None


def test_terminal_recording_writer_reopens_with_monotonic_offsets(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"

    first = TerminalRecordingWriter(recording_path, initial_rows=24, initial_cols=80)
    first.write_output(b"first\n")
    first.close()

    second = TerminalRecordingWriter(recording_path, initial_rows=24, initial_cols=80)
    second.write_output(b"second\n")
    second.close()

    offsets = [event["offset_ms"] for event in iter_terminal_recording(recording_path)]
    assert offsets == sorted(offsets)
    assert max(offsets[:2]) < min(offsets[2:])


def test_mirrored_terminal_recording_writer_keeps_plain_text_mirror(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    mirror_path = tmp_path / "agent-output.log"

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        mirror_path=mirror_path,
        initial_rows=30,
        initial_cols=100,
    )
    writer.write("hello\n")
    writer.write(b"world\n")
    writer.close()

    events = list(iter_terminal_recording(recording_path))
    payload = "".join(
        base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
        for event in events
        if event.get("event_type") == "output"
    )
    assert payload == "hello\nworld\n"
    assert mirror_path.read_text(encoding="utf-8") == "hello\nworld\n"


def test_mirrored_terminal_recording_writer_can_mirror_to_additional_recordings(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    secondary_path = tmp_path / "secondary-terminal-recording.jsonl"
    clock = IncrementingClock(now=100.0, step_seconds=0.001)

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[secondary_path],
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    writer.write("hello\n")
    writer.close()

    primary_events = list(iter_terminal_recording(recording_path))
    secondary_events = list(iter_terminal_recording(secondary_path))
    assert primary_events == secondary_events


def test_mirrored_terminal_recording_writer_invokes_output_callback(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    seen: list[bytes] = []

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        on_output=seen.append,
        initial_rows=24,
        initial_cols=80,
    )
    writer.write("hello\n")
    writer.close()

    assert seen == [b"hello\n"]


class ManualClock:
    def __init__(self, now: float) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class IncrementingClock:
    def __init__(self, *, now: float, step_seconds: float) -> None:
        self._now = now
        self._step_seconds = step_seconds
        self._first = True

    def __call__(self) -> float:
        if self._first:
            self._first = False
            return self._now
        self._now += self._step_seconds
        return self._now


def test_mirrored_terminal_recording_writer_preserves_per_path_base_offsets(
    tmp_path,
) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    aggregate_path = tmp_path / "aggregate-terminal-recording.jsonl"
    append_output_event(aggregate_path, "existing")
    clock = ManualClock(100.0)

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[aggregate_path],
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    clock.advance(0.001)
    writer.write("hello\n")
    writer.close()

    primary_events = list(iter_terminal_recording(recording_path))
    aggregate_events = list(iter_terminal_recording(aggregate_path))

    primary_offsets = [event["offset_ms"] for event in primary_events]
    aggregate_offsets = [event["offset_ms"] for event in aggregate_events[-2:]]
    assert primary_offsets == [0, 1]
    assert aggregate_offsets == [1, 2]


def test_add_mirror_recording_fans_subsequent_writes_only(tmp_path) -> None:
    """``add_mirror_recording`` attached mid-stream must NOT backfill
    earlier events into the new mirror — it sees only writes that
    happen after registration. Without this guarantee a slice attached
    to a cached pair would inherit the previous exchange's content."""
    recording_path = tmp_path / "pair.jsonl"
    slice_path = tmp_path / "slice.jsonl"
    clock = ManualClock(100.0)
    writer = MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    try:
        clock.advance(0.001)
        writer.write("BEFORE\n")
        # Attach the slice mid-stream.
        registered = writer.add_mirror_recording(slice_path, seed_resize=False)
        assert registered is True
        # Re-registering the same path is a no-op (returns False).
        assert writer.add_mirror_recording(slice_path, seed_resize=False) is False
        clock.advance(0.001)
        writer.write("AFTER-1\n")
        clock.advance(0.001)
        writer.write("AFTER-2\n")
    finally:
        writer.close()

    pair_events = list(iter_terminal_recording(recording_path))
    slice_events = list(iter_terminal_recording(slice_path))

    # Pair has resize + BEFORE + AFTER-1 + AFTER-2 = 4 events.
    assert len(pair_events) == 4
    # Slice has only AFTER-1 + AFTER-2 = 2 events (no resize because
    # seed_resize=False; no BEFORE because it was written pre-attach).
    assert len(slice_events) == 2
    # Decode payloads to confirm content alignment.
    import base64
    payloads = [
        base64.b64decode(e["data_b64"]).decode("utf-8")
        for e in slice_events
    ]
    assert payloads == ["AFTER-1\n", "AFTER-2\n"]


def test_add_mirror_recording_seed_resize_emits_initial_geometry(tmp_path) -> None:
    """When ``seed_resize=True``, the new mirror gets a synthetic resize
    event so a viewer attaching to the slice has initial PTY geometry.
    The geometry matches the writer's most recent shape."""
    recording_path = tmp_path / "pair.jsonl"
    slice_path = tmp_path / "slice.jsonl"
    writer = MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=40,
        initial_cols=120,
    )
    try:
        writer.write("first\n")
        registered = writer.add_mirror_recording(slice_path, seed_resize=True)
        assert registered is True
    finally:
        writer.close()

    slice_events = list(iter_terminal_recording(slice_path))
    # First event in the slice is the synthetic resize matching the
    # writer's current geometry. Subsequent agent writes (none here)
    # would follow.
    assert len(slice_events) == 1
    assert slice_events[0]["event_type"] == "resize"
    assert slice_events[0]["rows"] == 40
    assert slice_events[0]["cols"] == 120


def test_remove_mirror_recording_stops_fan_out(tmp_path) -> None:
    """``remove_mirror_recording`` detaches a mirror so subsequent
    writes no longer touch it. Idempotent — calling twice returns
    False the second time. Removing the canonical recording_path
    raises ``ValueError``."""
    recording_path = tmp_path / "pair.jsonl"
    slice_path = tmp_path / "slice.jsonl"
    writer = MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=24,
        initial_cols=80,
    )
    try:
        writer.add_mirror_recording(slice_path, seed_resize=False)
        writer.write("attached\n")
        slice_events_before_detach = list(iter_terminal_recording(slice_path))

        removed = writer.remove_mirror_recording(slice_path)
        assert removed is True
        # Idempotent: second remove returns False.
        assert writer.remove_mirror_recording(slice_path) is False

        writer.write("after-detach\n")
        slice_events_after_detach = list(iter_terminal_recording(slice_path))
        assert slice_events_after_detach == slice_events_before_detach, (
            "writer continued to mirror after remove_mirror_recording"
        )

        # Cannot remove the canonical recording.
        import pytest as _pt
        with _pt.raises(ValueError, match="cannot remove the canonical"):
            writer.remove_mirror_recording(recording_path)
    finally:
        writer.close()


def test_first_terminal_geometry_rejects_untrustworthy_dimensions(tmp_path) -> None:
    """``bool`` is an ``int`` subclass, so ``rows: true`` sized the viewer to 1."""
    import json

    recording_path = tmp_path / "terminal-recording.jsonl"
    rows = [
        {"schema_version": 1, "event_type": "resize", "offset_ms": 0,
         "rows": True, "cols": 120},
        {"schema_version": 1, "event_type": "resize", "offset_ms": 1,
         "rows": 0, "cols": 120},
        {"schema_version": 1, "event_type": "resize", "offset_ms": 2,
         "rows": 40, "cols": -3},
        {"schema_version": 1, "event_type": "resize", "offset_ms": 3,
         "rows": 40, "cols": 10_000_000},
        {"schema_version": 1, "event_type": "resize", "offset_ms": 4,
         "rows": 40, "cols": 120},
    ]
    recording_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert first_terminal_geometry(recording_path) == (40, 120)


class TestGeometryBoundsAgreeAcrossTheLanguageBoundary:
    """The browser viewer restates this policy; pin it so the two cannot drift.

    ``screen_dimension`` is the one owner deciding what a trustworthy ``resize``
    row carries, and the Python viewport and viewer geometry lookup both call
    it. The dashboard's replay runs in a browser and cannot import it, so
    ``session_replay.js`` restates the same rule — which makes the bounds a
    duplicated constant, and duplicated constants drift. This reads the JS and
    fails if either side moves (#7141 round 4).

    Semantics are pinned on the JS side by
    ``tests/js/session_replay_geometry.test.js``; this pins only the numbers.
    """

    def _session_replay_source(self) -> str:
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "issue_orchestrator"
            / "static"
            / "js"
            / "dashboard"
            / "session_replay.js"
        )
        return source.read_text(encoding="utf-8")

    def test_the_javascript_row_bound_matches_the_python_owner(self) -> None:
        assert (
            f"const SESSION_REPLAY_MAX_ROWS = {MAX_TERMINAL_ROWS};"
            in self._session_replay_source()
        )

    def test_the_javascript_column_bound_matches_the_python_owner(self) -> None:
        assert (
            f"const SESSION_REPLAY_MAX_COLS = {MAX_TERMINAL_COLS};"
            in self._session_replay_source()
        )

    def test_the_javascript_names_its_python_counterpart(self) -> None:
        """A reader of either side must be able to find the other."""
        assert "screen_dimension" in self._session_replay_source()


class TestRecordingOutputEdges:
    """The first and last PTY bytes a recording holds, read row by row (#9587).

    The rows are scanned whole, so the window is exact however the PTY reads
    chunked the output, and bounded however long the session ran.
    """

    EDGE = 8192

    @staticmethod
    def _record(path: Path, chunks: list[bytes]) -> Path:
        writer = TerminalRecordingWriter(path, initial_rows=40, initial_cols=120)
        for chunk in chunks:
            writer.write_output(chunk)
        writer.close()
        return path

    def test_a_short_recording_comes_back_whole(self, tmp_path: Path) -> None:
        path = self._record(tmp_path / "terminal-recording.jsonl", [b"Login ", b"expired"])

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges == TerminalOutputEdges(head=b"Login expired", tail=None)

    def test_output_longer_than_one_end_but_not_two_comes_back_whole(
        self, tmp_path: Path
    ) -> None:
        """The two scans meet: no gap, so no separator splits a phrase in two."""
        path = self._record(
            tmp_path / "terminal-recording.jsonl", [b"a" * 6000, b"b" * 6000]
        )

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges == TerminalOutputEdges(head=b"a" * 6000 + b"b" * 6000, tail=None)

    @pytest.mark.parametrize("seed", range(4))
    def test_each_end_is_exact_whatever_the_chunk_sizes(
        self, seed: int, tmp_path: Path
    ) -> None:
        rng = random.Random(seed)
        chunks: list[bytes] = []
        while sum(map(len, chunks)) < 96 * 1024:
            if rng.random() < 0.5:
                # A run of single-byte reads: a row's envelope per PTY byte.
                chunks.extend(rng.randbytes(1) for _ in range(rng.randint(1, 300)))
            else:
                chunks.append(rng.randbytes(rng.randint(1, 4096)))
        path = self._record(tmp_path / "terminal-recording.jsonl", chunks)
        output = b"".join(chunks)

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges.head == output[: self.EDGE]
        assert edges.tail == output[-self.EDGE :]

    def test_a_banner_followed_by_tiny_rows_is_still_in_the_tail(
        self, tmp_path: Path
    ) -> None:
        """Inside the last edge of output, yet far more than 4x the edge of raw file.

        Each single-byte row costs its whole JSON envelope, so a fixed raw window
        (the first fix read 4x the edge) misses a banner that the terminal shows
        just a few hundred bytes from the end.
        """
        banner = b"Login expired \xc2\xb7 Please run /login\r\n"
        path = self._record(
            tmp_path / "terminal-recording.jsonl",
            [b"x" * 40_000, banner] + [b"."] * 2000,
        )
        raw = path.read_bytes()
        banner_row_end = raw.index(b"\n", raw.index(base64.b64encode(banner))) + 1
        assert len(raw) - banner_row_end > 4 * self.EDGE, "the shape proves nothing"

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges.tail is not None
        assert banner in edges.tail

    def test_rows_that_carry_no_output_are_skipped(self, tmp_path: Path) -> None:
        """Resizes, bad payloads, non-JSON and the row a live writer is still writing."""
        path = tmp_path / "terminal-recording.jsonl"
        writer = TerminalRecordingWriter(path, initial_rows=40, initial_cols=120)
        writer.write_output(b"kept ")
        writer.close()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_type": "output", "data_b64": "@@"}) + "\n")
            handle.write("not a json row\n")
            handle.write(json.dumps({"event_type": "output", "data_b64": None}) + "\n")
        writer = TerminalRecordingWriter(path)
        writer.write_output(b"too")
        writer.close()
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"data_b64": "dW5maW5pc2hlZA')  # no newline yet

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges == TerminalOutputEdges(head=b"kept too", tail=None)

    def test_the_unfinished_last_row_of_a_long_recording_is_skipped(
        self, tmp_path: Path
    ) -> None:
        path = self._record(
            tmp_path / "terminal-recording.jsonl",
            [b"HEAD-MARK"] + [b"ordinary output line\r\n"] * 5000 + [b"TAIL-MARK"],
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"data_b64": "dW5maW5pc2hlZA')

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges.head.startswith(b"HEAD-MARK")
        assert edges.tail is not None
        assert edges.tail.endswith(b"TAIL-MARK")
        assert len(edges.head) == len(edges.tail) == self.EDGE

    def test_rows_without_output_spend_a_bounded_budget(self, tmp_path: Path) -> None:
        """Past its budget of rows that carry no output, an end stops short.

        The window shrinks rather than the read growing with the file: the
        banner behind that many resizes is not reached from the tail.
        """
        path = tmp_path / "terminal-recording.jsonl"
        writer = TerminalRecordingWriter(path)
        writer.write_output(b"x" * (3 * self.EDGE))
        writer.write_output(b"BANNER")
        resize_row_bytes = len(
            json.dumps(
                {"cols": 120, "event_type": "resize", "offset_ms": 0, "rows": 40,
                 "schema_version": 1},
                sort_keys=True,
            )
        ) + 1
        for _ in range(self.EDGE * MAX_ROW_BYTES_PER_OUTPUT_BYTE // resize_row_bytes + 1):
            writer.write_resize(rows=40, cols=120, elapsed_ms=0)
        writer.write_output(b"TAIL")
        writer.close()

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges.tail == b"TAIL"

    def test_a_row_longer_than_the_budget_ends_the_scan(self, tmp_path: Path) -> None:
        """No PTY write makes a row that long: the file is not a recording there."""
        path = tmp_path / "terminal-recording.jsonl"
        writer = TerminalRecordingWriter(path)
        writer.write_output(b"HEAD")
        writer.close()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("z" * (self.EDGE * MAX_ROW_BYTES_PER_OUTPUT_BYTE + 10) + "\n")
        writer = TerminalRecordingWriter(path)
        writer.write_output(b"TAIL")
        writer.close()

        edges = read_recording_output_edges(path, self.EDGE)

        assert edges == TerminalOutputEdges(head=b"HEAD", tail=b"TAIL")

    def test_an_unreadable_recording_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            read_recording_output_edges(tmp_path / "missing.jsonl", self.EDGE)


class TestTheRowBudgetHoldsForTheWriter:
    """``MAX_ROW_BYTES_PER_OUTPUT_BYTE`` is derived from the writer's row format.

    The exact-window guarantee rests on two writer facts: every output row
    carries at least one PTY byte, and no row spends more raw bytes per PTY byte
    than the budget allows. Both are checked against the writer itself.
    """

    #: A session clock past any real session: the widest offset_ms field.
    LATE_MS = 10**13 - 1

    @pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 100, 4096])
    def test_no_output_row_costs_more_than_the_budget_per_byte(
        self, size: int, tmp_path: Path
    ) -> None:
        path = tmp_path / "terminal-recording.jsonl"
        writer = TerminalRecordingWriter(path)
        writer.write_output(b"\xff" * size, elapsed_ms=self.LATE_MS)
        writer.close()

        assert len(path.read_bytes()) <= size * MAX_ROW_BYTES_PER_OUTPUT_BYTE

    def test_the_writer_records_no_empty_output_row(self, tmp_path: Path) -> None:
        path = tmp_path / "terminal-recording.jsonl"
        writer = TerminalRecordingWriter(path)
        writer.write_output(b"")
        writer.write(b"")
        writer.close()

        assert path.read_bytes() == b""
