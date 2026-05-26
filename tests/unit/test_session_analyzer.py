"""Tests for session_analyzer — analyze() + write/load round-trip."""

import json
from pathlib import Path

import pytest

from issue_orchestrator.control.session_analyzer import (
    ANALYSIS_FILENAME,
    analyze,
    load_analysis,
    write_analysis,
)
from issue_orchestrator.domain.run_manifest import MANIFEST_FILENAME, RunManifest


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions" / "20260211-120000Z__issue-42"
    d.mkdir(parents=True)
    return d


def _manifest(run_dir: Path, **overrides: object) -> RunManifest:
    """Create a minimal manifest, writing it to disk and returning the loaded object."""
    data: dict = {"session_name": "issue-42", "run_id": "r1", **overrides}
    (run_dir / MANIFEST_FILENAME).write_text(json.dumps(data))
    return RunManifest.load(run_dir)


# ------------------------------------------------------------------
# Blocked
# ------------------------------------------------------------------

class TestBlocked:
    def test_headline_includes_reason(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="blocked", blocked_reason="Merge conflict")
        a = analyze(m)
        assert "Merge conflict" in a.headline
        assert "blocked" in a.headline.lower()

    def test_detail_includes_attempted(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="blocked", blocked_reason="x", attempted="Tried rebasing")
        a = analyze(m)
        assert a.detail is not None
        assert "Tried rebasing" in a.detail

    def test_detail_includes_blocked_by(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="blocked", blocked_reason="x", blocked_by=[10, 20])
        a = analyze(m)
        assert a.detail is not None
        assert "#10" in a.detail
        assert "#20" in a.detail

    def test_suggestions_present(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="blocked", blocked_reason="x")
        a = analyze(m)
        assert len(a.suggestions) > 0

    def test_log_excerpt_from_tail(self, run_dir: Path) -> None:
        tail = "\n".join(f"line {i}" for i in range(20))
        m = _manifest(run_dir, outcome="blocked", blocked_reason="x", log_tail=tail)
        a = analyze(m)
        assert a.log_excerpt is not None
        assert "line 19" in a.log_excerpt


# ------------------------------------------------------------------
# Timeout
# ------------------------------------------------------------------

class TestTimeout:
    def test_headline_with_runtime_and_limit(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="timeout", runtime_minutes=45.2, timeout_minutes=45)
        a = analyze(m)
        assert "45 min" in a.headline
        assert "limit: 45" in a.headline

    def test_headline_accepts_session_status_value(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="timed_out", runtime_minutes=90.0, timeout_minutes=90)
        a = analyze(m)
        assert "Timed out after 90 min" in a.headline

    def test_headline_runtime_only(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="timeout", runtime_minutes=30.0)
        a = analyze(m)
        assert "30 min" in a.headline

    def test_headline_no_runtime(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="timeout")
        a = analyze(m)
        assert "timed out" in a.headline.lower()

    def test_suggestions_present(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="timeout")
        a = analyze(m)
        assert len(a.suggestions) >= 2


# ------------------------------------------------------------------
# Failed
# ------------------------------------------------------------------

class TestFailed:
    def test_headline(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="failed")
        a = analyze(m)
        assert "without completion command" in a.headline.lower()

    def test_detail_uses_problems(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="failed", problems="Segfault in build")
        a = analyze(m)
        assert a.detail is not None
        assert "Segfault" in a.detail

    def test_detail_default_when_no_problems(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="failed")
        a = analyze(m)
        assert a.detail is not None
        assert "crash" in a.detail.lower() or "interruption" in a.detail.lower()


# ------------------------------------------------------------------
# Needs human
# ------------------------------------------------------------------

class TestNeedsHuman:
    def test_headline_includes_question(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="needs_human", question="Which API endpoint?")
        a = analyze(m)
        assert "Which API endpoint?" in a.headline

    def test_suggestion(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="needs_human", question="q")
        a = analyze(m)
        assert any("GitHub" in s for s in a.suggestions)


# ------------------------------------------------------------------
# Completed
# ------------------------------------------------------------------

class TestCompleted:
    def test_normal_completion(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", implementation="Added retry logic")
        a = analyze(m)
        assert "Added retry logic" in a.headline

    def test_completion_with_problems(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", implementation="Done", problems="Flaky test")
        a = analyze(m)
        assert a.detail is not None
        assert "Flaky test" in a.detail

    def test_review_approved(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", review_summary="LGTM")
        a = analyze(m)
        assert "approved" in a.headline.lower()
        assert "LGTM" in a.headline

    def test_review_changes_requested(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", review_issues="Missing tests")
        a = analyze(m)
        assert "Missing tests" in a.headline

    def test_review_changes_with_risk(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", review_issues="Missing tests", risk_level="high")
        a = analyze(m)
        assert a.detail is not None
        assert "high" in a.detail

    def test_validation_failed(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", validation_passed=False, validation_reason="lint errors")
        a = analyze(m)
        assert "Validation failed" in a.headline
        assert "lint errors" in a.headline


# ------------------------------------------------------------------
# Unknown outcome
# ------------------------------------------------------------------

class TestUnknownOutcome:
    def test_missing_outcome(self, run_dir: Path) -> None:
        m = _manifest(run_dir)
        a = analyze(m)
        assert "unknown" in a.headline.lower()

    def test_novel_outcome(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="something_new")
        a = analyze(m)
        assert "something_new" in a.headline


# ------------------------------------------------------------------
# Truncation
# ------------------------------------------------------------------

class TestTruncation:
    def test_headline_truncated(self, run_dir: Path) -> None:
        long_reason = "x" * 200
        m = _manifest(run_dir, outcome="blocked", blocked_reason=long_reason)
        a = analyze(m)
        assert len(a.headline) <= 120

    def test_detail_truncated(self, run_dir: Path) -> None:
        long_attempted = "y" * 500
        m = _manifest(run_dir, outcome="blocked", blocked_reason="r", attempted=long_attempted)
        a = analyze(m)
        assert a.detail is not None
        assert len(a.detail) <= 300


# ------------------------------------------------------------------
# write / load round-trip
# ------------------------------------------------------------------

class TestWriteLoad:
    def test_round_trip(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="blocked", blocked_reason="Merge conflict")
        a = analyze(m)
        write_analysis(run_dir, a)

        loaded = load_analysis(run_dir)
        assert loaded is not None
        assert loaded.headline == a.headline
        assert loaded.detail == a.detail
        assert loaded.suggestions == a.suggestions

    def test_load_missing_returns_none(self, run_dir: Path) -> None:
        assert load_analysis(run_dir) is None

    def test_write_creates_file(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", implementation="Done")
        a = analyze(m)
        write_analysis(run_dir, a)
        assert (run_dir / ANALYSIS_FILENAME).exists()

    def test_json_structure(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="timeout", runtime_minutes=30.0, timeout_minutes=30)
        a = analyze(m)
        write_analysis(run_dir, a)

        raw = json.loads((run_dir / ANALYSIS_FILENAME).read_text())
        assert "headline" in raw
        assert "suggestions" in raw
        assert isinstance(raw["suggestions"], list)

    def test_none_fields_omitted(self, run_dir: Path) -> None:
        m = _manifest(run_dir, outcome="completed", implementation="Done")
        a = analyze(m)
        assert a.log_excerpt is None
        write_analysis(run_dir, a)

        raw = json.loads((run_dir / ANALYSIS_FILENAME).read_text())
        assert "log_excerpt" not in raw
