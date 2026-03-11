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
