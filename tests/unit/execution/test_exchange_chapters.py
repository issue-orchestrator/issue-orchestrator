"""Chapter sidecar (chapters.json) writer behavior."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.domain.exchange_chapter import (
    CHAPTER_SCHEMA_VERSION,
    CHAPTER_SECTION_FEEDBACK,
    CHAPTER_SECTION_PROMPT,
    ExchangeChapterSidecar,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def _start_run(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    output = FileSystemSessionOutput()
    run = output.start_run(worktree, "review-exchange-359", issue_number=359)
    return run.run_dir


class TestChapterSidecarWriter:
    def test_first_chapter_creates_sidecar_with_metadata(self, tmp_path: Path) -> None:
        run_dir = _start_run(tmp_path)
        output = FileSystemSessionOutput()

        sidecar_path = output.record_exchange_chapter(
            run_dir,
            role="reviewer",
            exchange_run_id="exch-1",
            issue_number=359,
            cycle_index=1,
            section=CHAPTER_SECTION_PROMPT,
            recording_event_index=0,
            recorded_at="2026-05-02T18:00:00Z",
            label="Round 1 reviewer prompt",
        )

        assert sidecar_path == run_dir / "reviewer" / "chapters.json"
        payload = json.loads(sidecar_path.read_text())
        assert payload["schema_version"] == CHAPTER_SCHEMA_VERSION
        assert payload["role"] == "reviewer"
        assert payload["exchange_run_id"] == "exch-1"
        assert payload["issue_number"] == 359
        assert len(payload["chapters"]) == 1
        ch = payload["chapters"][0]
        assert ch["cycle_index"] == 1
        assert ch["section"] == CHAPTER_SECTION_PROMPT
        assert ch["recording_event_index"] == 0
        assert ch["recorded_at"] == "2026-05-02T18:00:00Z"
        assert ch["label"] == "Round 1 reviewer prompt"

    def test_subsequent_chapters_append_in_order(self, tmp_path: Path) -> None:
        run_dir = _start_run(tmp_path)
        output = FileSystemSessionOutput()
        kwargs = dict(
            run_dir=run_dir, role="reviewer",
            exchange_run_id="exch-1", issue_number=359,
        )

        output.record_exchange_chapter(
            **kwargs, cycle_index=1, section=CHAPTER_SECTION_PROMPT,
            recording_event_index=0, recorded_at="t1", label="round 1 prompt",
        )
        output.record_exchange_chapter(
            **kwargs, cycle_index=1, section=CHAPTER_SECTION_FEEDBACK,
            recording_event_index=42, recorded_at="t2", label="round 1 feedback",
        )
        output.record_exchange_chapter(
            **kwargs, cycle_index=2, section=CHAPTER_SECTION_PROMPT,
            recording_event_index=80, recorded_at="t3", label="round 2 prompt",
        )

        sidecar = output.read_exchange_chapters(run_dir, role="reviewer")
        assert sidecar is not None
        assert [(c.cycle_index, c.section, c.recording_event_index) for c in sidecar.chapters] == [
            (1, CHAPTER_SECTION_PROMPT, 0),
            (1, CHAPTER_SECTION_FEEDBACK, 42),
            (2, CHAPTER_SECTION_PROMPT, 80),
        ]

    def test_each_role_writes_its_own_sidecar(self, tmp_path: Path) -> None:
        run_dir = _start_run(tmp_path)
        output = FileSystemSessionOutput()

        output.record_exchange_chapter(
            run_dir, role="reviewer", exchange_run_id="exch-1", issue_number=359,
            cycle_index=1, section=CHAPTER_SECTION_PROMPT,
            recording_event_index=0, recorded_at="t", label="r-prompt",
        )
        output.record_exchange_chapter(
            run_dir, role="coder", exchange_run_id="exch-1", issue_number=359,
            cycle_index=1, section=CHAPTER_SECTION_PROMPT,
            recording_event_index=0, recorded_at="t", label="c-prompt",
        )

        reviewer_sidecar = output.read_exchange_chapters(run_dir, role="reviewer")
        coder_sidecar = output.read_exchange_chapters(run_dir, role="coder")
        assert reviewer_sidecar is not None
        assert coder_sidecar is not None
        assert reviewer_sidecar.chapters[0].label == "r-prompt"
        assert coder_sidecar.chapters[0].label == "c-prompt"

    def test_read_returns_none_when_sidecar_absent(self, tmp_path: Path) -> None:
        run_dir = _start_run(tmp_path)
        output = FileSystemSessionOutput()

        assert output.read_exchange_chapters(run_dir, role="reviewer") is None

    def test_read_returns_none_when_sidecar_malformed(self, tmp_path: Path) -> None:
        run_dir = _start_run(tmp_path)
        output = FileSystemSessionOutput()
        bogus_path = run_dir / "reviewer" / "chapters.json"
        bogus_path.parent.mkdir(parents=True, exist_ok=True)
        bogus_path.write_text("{not json")

        assert output.read_exchange_chapters(run_dir, role="reviewer") is None

    def test_writer_recovers_from_pre_existing_corrupt_sidecar(self, tmp_path: Path) -> None:
        run_dir = _start_run(tmp_path)
        output = FileSystemSessionOutput()
        sidecar_path = run_dir / "reviewer" / "chapters.json"
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text("{garbage")

        # Recovery: corrupt file is replaced with a fresh sidecar that starts
        # at one chapter — the prior corruption is dropped (better that than
        # crashing the exchange).
        output.record_exchange_chapter(
            run_dir, role="reviewer", exchange_run_id="exch-1", issue_number=359,
            cycle_index=1, section=CHAPTER_SECTION_PROMPT,
            recording_event_index=0, recorded_at="t", label="recovered",
        )
        sidecar = output.read_exchange_chapters(run_dir, role="reviewer")
        assert sidecar is not None
        assert len(sidecar.chapters) == 1
        assert sidecar.chapters[0].label == "recovered"


class TestExchangeChapterSidecarSerialization:
    def test_round_trip_through_payload(self) -> None:
        sidecar = ExchangeChapterSidecar.from_payload({
            "schema_version": 1,
            "role": "coder",
            "exchange_run_id": "exch-7",
            "issue_number": 4057,
            "chapters": [
                {
                    "cycle_index": 2,
                    "section": "feedback",
                    "recording_event_index": 100,
                    "recorded_at": "2026-05-02T18:00:00Z",
                    "label": "round 2 coder feedback",
                },
            ],
        })
        payload = sidecar.to_payload()
        restored = ExchangeChapterSidecar.from_payload(payload)
        assert restored == sidecar

    def test_from_payload_rejects_non_list_chapters(self) -> None:
        import pytest
        with pytest.raises(ValueError, match="must be a list"):
            ExchangeChapterSidecar.from_payload({
                "role": "coder", "exchange_run_id": "x", "issue_number": 1,
                "chapters": "oops",
            })
