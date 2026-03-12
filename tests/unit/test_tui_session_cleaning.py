"""Test TUI session log cleaning against real captured output.

Uses a real ui-session.log from a Claude Code interactive session, fed through
CleaningLogWriter as a byte stream (the same way pexpect delivers PTY data),
to validate that the cleaning pipeline filters TUI noise while preserving
meaningful content.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.infra.terminal_cleaning import CleaningLogWriter

_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "real_tui_session.log"
_FIXTURE_4057 = Path(__file__).resolve().parent.parent / "fixtures" / "real_tui_session_4057.log"


def _stream_through_writer(fixture: Path, tmp_path: Path) -> list[str]:
    """Feed fixture through CleaningLogWriter as a byte stream, return cleaned lines."""
    output = tmp_path / "cleaned.log"
    writer = CleaningLogWriter(output)
    # Feed line-by-line as bytes (simulating PTY chunks with \n terminators)
    raw_bytes = fixture.read_bytes()
    writer.write(raw_bytes)
    writer.close()
    return [l for l in output.read_text().splitlines() if l.strip()]


class TestRealTuiSessionCleaning:
    """Validate CleaningLogWriter against a real 2071-line TUI session log."""

    def test_fixture_exists(self):
        assert _FIXTURE.exists(), f"Fixture missing: {_FIXTURE}"

    def test_massive_reduction(self, tmp_path: Path):
        """2071 raw lines should collapse dramatically through CleaningLogWriter."""
        raw_line_count = len(_FIXTURE.read_text().splitlines())
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        assert raw_line_count > 2000, f"Fixture too small: {raw_line_count} lines"
        assert len(cleaned) < 95, (
            f"Cleaned output still has {len(cleaned)} lines — too much noise.\n"
            f"Lines:\n" + "\n".join(f"  {i}: {l}" for i, l in enumerate(cleaned))
        )

    def test_preserves_meaningful_content(self, tmp_path: Path):
        """Key content lines must survive cleaning."""
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        content = "\n".join(cleaned)

        # Commit messages
        assert "fix: skip validation under orchestrator" in content
        assert "surface provider circuit breaker status" in content

        # Work instruction
        assert "Work on issue #4057" in content

        # Agent thinking
        assert "evaluate the existing work" in content

        # Diff content (the actual code changes)
        assert "Skip under orchestrator" in content
        assert "under_orchestrator" in content

        # Completion message
        assert "coding-done completed successfully" in content

    def test_filters_all_clauding_lines(self, tmp_path: Path):
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        for line in cleaned:
            assert "Clauding" not in line, f"Clauding leaked: {line!r}"

    def test_filters_pure_digit_lines(self, tmp_path: Path):
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        for line in cleaned:
            stripped = line.strip()
            if stripped.isdigit() and len(stripped) <= 5:
                raise AssertionError(f"Pure digit line leaked: {line!r}")

    def test_filters_spinner_symbols(self, tmp_path: Path):
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        for line in cleaned:
            if line.strip() == "⏺":
                raise AssertionError(f"Standalone ⏺ leaked: {line!r}")

    def test_filters_timeout_indicators(self, tmp_path: Path):
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        for line in cleaned:
            if "timeout" in line and "·" in line and line.strip().startswith("("):
                raise AssertionError(f"Timeout indicator leaked: {line!r}")

    def test_filters_tui_chrome(self, tmp_path: Path):
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        for line in cleaned:
            stripped = line.strip()
            if "ctrl+o" in stripped.lower():
                raise AssertionError(f"TUI chrome leaked: {line!r}")
            if "ctrl+c" in stripped.lower() and "interrupt" in stripped.lower():
                raise AssertionError(f"TUI chrome leaked: {line!r}")

    def test_filters_partial_word_fragments(self, tmp_path: Path):
        """Short meaningless fragments like 'Cu', 'Cl', 'ld3', 'terrupt' should be filtered."""
        cleaned = _stream_through_writer(_FIXTURE, tmp_path)
        _KNOWN_FRAGMENTS = {"Cu", "Cl", "ld3", "din40", "g…40", "terrupt", "ding…↓"}
        for line in cleaned:
            stripped = line.strip()
            if stripped in _KNOWN_FRAGMENTS:
                raise AssertionError(f"Fragment leaked: {line!r}")


class TestRealTuiSession4057Cleaning:
    """Validate CleaningLogWriter against the 4057 interactive coding session log.

    This session log captured new noise patterns: Lollygagging spinner,
    'current work' status bar, 'Update available' notifications, and
    partial word fragments from cursor-repositioned TUI rendering.
    """

    def test_fixture_exists(self):
        assert _FIXTURE_4057.exists(), f"Fixture missing: {_FIXTURE_4057}"

    def test_significant_reduction(self, tmp_path: Path):
        """623 raw lines should have all TUI noise filtered (>50% reduction)."""
        raw_line_count = len(_FIXTURE_4057.read_text().splitlines())
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        assert raw_line_count > 500, f"Fixture too small: {raw_line_count} lines"
        # Remaining lines are real content (code diffs, tool output, agent summary).
        # Verify at least 50% reduction (spinner/status noise removed).
        reduction_pct = 100 - len(cleaned) * 100 // raw_line_count
        assert reduction_pct >= 50, (
            f"Only {reduction_pct}% reduction ({raw_line_count} → {len(cleaned)}) — "
            f"TUI noise not being filtered.\n"
            f"Sample lines:\n" + "\n".join(f"  {i}: {l}" for i, l in enumerate(cleaned[:20]))
        )

    def test_filters_lollygagging(self, tmp_path: Path):
        """All Lollygagging spinner lines must be filtered."""
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        for line in cleaned:
            lower = line.lower()
            assert "lollygagging" not in lower, f"Lollygagging leaked: {line!r}"

    def test_filters_current_work(self, tmp_path: Path):
        """TUI 'current work' status bar lines must be filtered."""
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        for line in cleaned:
            stripped = line.strip().lower().replace(" ", "")
            assert stripped != "currentwork", f"'current work' leaked: {line!r}"

    def test_filters_update_available(self, tmp_path: Path):
        """'Update available! Run: brew up…' notifications must be filtered."""
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        for line in cleaned:
            assert "Update" not in line or "available" not in line or "brew" not in line, (
                f"Update notification leaked: {line!r}"
            )

    def test_filters_lollygagging_fragments(self, tmp_path: Path):
        """Partial fragments of 'lollygagging' from cursor repositioning must be filtered."""
        _LOLLY_FRAGMENTS = {"ollgin", "ollng…", "Lolyg", "aggng…", "llygag", "ollygagging…"}
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        for line in cleaned:
            stripped = line.strip().rstrip("↑↓")
            if stripped in _LOLLY_FRAGMENTS:
                raise AssertionError(f"Lollygagging fragment leaked: {line!r}")

    def test_preserves_code_diffs(self, tmp_path: Path):
        """Actual code diff content must survive cleaning."""
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        content = "\n".join(cleaned)
        # The agent was working on circuit breaker UI
        assert "provider" in content.lower()
        assert "circuit" in content.lower() or "outage" in content.lower()

    def test_preserves_implementation_summary(self, tmp_path: Path):
        """The agent's implementation summary should survive cleaning."""
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        content = "\n".join(cleaned)
        assert "Implementation complete" in content or "ProviderCircuitBreaker" in content

    def test_filters_waiting_for_input(self, tmp_path: Path):
        """'Claude is waiting for your input' prompt must be filtered."""
        cleaned = _stream_through_writer(_FIXTURE_4057, tmp_path)
        for line in cleaned:
            assert "waiting for your input" not in line.lower(), (
                f"Waiting prompt leaked: {line!r}"
            )
