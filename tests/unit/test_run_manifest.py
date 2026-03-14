"""Tests for RunManifest dataclass — load, save, enrichment."""

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.run_manifest import RunManifest, MANIFEST_FILENAME


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions" / "20260211-120000Z__issue-42"
    d.mkdir(parents=True)
    return d


def _write_manifest(run_dir: Path, data: dict) -> Path:
    data = dict(data)
    data.setdefault("run_dir", str(run_dir))
    log_path = data.setdefault("log_path", str(run_dir / "terminal-recording.jsonl"))
    data.setdefault(
        "artifacts",
        {
            "terminal_recording": {
                "kind": "terminal_recording",
                "path": log_path,
                "content_type": "application/x-ndjson",
            },
        },
    )
    p = run_dir / MANIFEST_FILENAME
    p.write_text(json.dumps(data))
    return p


# ------------------------------------------------------------------
# load / save round-trip
# ------------------------------------------------------------------

class TestLoadSave:
    def test_round_trip_preserves_fields(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {
            "session_name": "issue-42",
            "run_id": "20260211-120000Z",
            "run_dir": str(run_dir),
            "issue_number": 42,
            "started_at": "2026-02-11T12:00:00Z",
        })
        m = RunManifest.load(run_dir)
        assert m.session_name == "issue-42"
        assert m.run_id == "20260211-120000Z"
        assert m.issue_number == 42
        assert m.run_dir == run_dir

        m.save()
        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        assert raw["session_name"] == "issue-42"
        assert raw["issue_number"] == 42

    def test_unknown_fields_preserved(self, run_dir: Path) -> None:
        """Old manifests may have fields we don't model — they must survive."""
        _write_manifest(run_dir, {
            "session_name": "issue-42",
            "run_id": "r1",
            "run_dir": str(run_dir),
            "some_future_field": "hello",
            "retention_pinned": True,
        })
        m = RunManifest.load(run_dir)
        assert m._extra["some_future_field"] == "hello"
        assert m._extra["retention_pinned"] is True

        m.save()
        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        assert raw["some_future_field"] == "hello"
        assert raw["retention_pinned"] is True

    def test_missing_optional_fields_default_to_none(self, run_dir: Path) -> None:
        """Minimal manifest (e.g. old format) loads cleanly."""
        _write_manifest(run_dir, {
            "session_name": "issue-1",
            "run_id": "r1",
        })
        m = RunManifest.load(run_dir)
        assert m.session_name == "issue-1"
        assert m.implementation is None
        assert m.log_tail is None
        assert m.blocked_by is None

    def test_none_fields_omitted_from_json(self, run_dir: Path) -> None:
        """to_dict() skips None values to keep the file clean."""
        m = RunManifest(session_name="s", run_id="r", run_dir=run_dir)
        d = m.to_dict()
        assert "implementation" not in d
        assert "log_tail" not in d
        assert "session_name" in d

    def test_load_file_not_found(self, run_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            RunManifest.load(run_dir)


# ------------------------------------------------------------------
# update
# ------------------------------------------------------------------

class TestUpdate:
    def test_update_sets_and_saves(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {
            "session_name": "issue-42",
            "run_id": "r1",
        })
        m = RunManifest.load(run_dir)
        m.update(implementation="Added retry logic")
        assert m.implementation == "Added retry logic"

        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        assert raw["implementation"] == "Added retry logic"

    def test_update_rejects_unknown_field(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)
        with pytest.raises(TypeError, match="no_such_field"):
            m.update(no_such_field="bad")


# ------------------------------------------------------------------
# enrich_from_completion_record
# ------------------------------------------------------------------

class TestEnrichFromCompletionRecord:
    def test_copies_all_relevant_fields(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)

        class FakeRecord:
            implementation = "Built the feature"
            problems = "Flaky test"
            attempted = "Tried 3 approaches"
            blocked_reason = "API limit"
            blocked_by = [10, 20]
            question = "Which endpoint?"
            review_summary = "Looks good"
            review_issues = "Missing tests"
            risk_level = "low"

        m.enrich_from_completion_record(FakeRecord())
        assert m.implementation == "Built the feature"
        assert m.problems == "Flaky test"
        assert m.attempted == "Tried 3 approaches"
        assert m.blocked_reason == "API limit"
        assert m.blocked_by == [10, 20]
        assert m.question == "Which endpoint?"
        assert m.review_summary == "Looks good"
        assert m.review_issues == "Missing tests"
        assert m.risk_level == "low"

    def test_skips_none_values(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)

        class FakeRecord:
            implementation = "Done"
            problems = None
            attempted = None
            blocked_reason = None
            blocked_by = None
            question = None
            review_summary = None
            review_issues = None
            risk_level = None

        m.enrich_from_completion_record(FakeRecord())
        assert m.implementation == "Done"
        assert m.problems is None

    def test_does_not_overwrite_existing_with_none(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {"session_name": "s", "run_id": "r"})
        m = RunManifest.load(run_dir)
        m.implementation = "Original"

        class FakeRecord:
            implementation = None
            problems = None
            attempted = None
            blocked_reason = None
            blocked_by = None
            question = None
            review_summary = None
            review_issues = None
            risk_level = None

        m.enrich_from_completion_record(FakeRecord())
        assert m.implementation == "Original"


# ------------------------------------------------------------------
# to_dict
# ------------------------------------------------------------------

class TestToDict:
    def test_run_dir_serialised_as_string(self, run_dir: Path) -> None:
        m = RunManifest(session_name="s", run_id="r", run_dir=run_dir)
        d = m.to_dict()
        assert isinstance(d["run_dir"], str)

    def test_extra_fields_merged(self, run_dir: Path) -> None:
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
            "custom_key": 123,
        })
        m = RunManifest.load(run_dir)
        d = m.to_dict()
        assert d["custom_key"] == 123
        assert d["session_name"] == "s"

    def test_known_fields_override_extra(self, run_dir: Path) -> None:
        """If an _extra key collides with a known field, known field wins."""
        _write_manifest(run_dir, {
            "session_name": "s",
            "run_id": "r",
        })
        m = RunManifest.load(run_dir)
        m._extra["session_name"] = "stale"
        d = m.to_dict()
        assert d["session_name"] == "s"
