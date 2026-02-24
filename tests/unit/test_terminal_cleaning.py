"""Tests for terminal output cleaning utilities and CleaningLogWriter."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.infra.terminal_cleaning import (
    CleaningLogWriter,
    clean_terminal_line,
    dedupe_consecutive_lines,
    extract_stream_json_text,
    is_spinner_fragment,
    strip_ansi_codes,
)


# ---------------------------------------------------------------------------
# strip_ansi_codes
# ---------------------------------------------------------------------------


class TestStripAnsiCodes:
    """Test ANSI escape sequence stripping."""

    def test_strips_color_codes(self):
        # SGR color codes
        assert strip_ansi_codes("\x1b[38;2;215;119;87mHello\x1b[39m") == "Hello"
        assert strip_ansi_codes("\x1b[1mBold\x1b[22m") == "Bold"
        assert strip_ansi_codes("\x1b[2mDim\x1b[22m") == "Dim"

        # Red text
        assert strip_ansi_codes("\x1b[31mError\x1b[0m") == "Error"
        # Bold green
        assert strip_ansi_codes("\x1b[1;32mSuccess\x1b[0m") == "Success"
        # 256-color
        assert strip_ansi_codes("\x1b[38;5;196mBright Red\x1b[0m") == "Bright Red"
        # 24-bit RGB (Claude Code uses this)
        assert strip_ansi_codes("\x1b[38;2;215;119;87m✶\x1b[0m") == "✶"

    def test_strips_cursor_movement(self):
        assert strip_ansi_codes("\x1b[6AMove up") == "Move up"
        assert strip_ansi_codes("\x1b[2CMove right") == "Move right"
        assert strip_ansi_codes("\x1b[K") == ""  # Erase to end of line
        # Cursor down, right, left
        assert strip_ansi_codes("Start\x1b[2B\x1b[1C\x1b[3DEnd") == "StartEnd"

    def test_strips_private_mode_sequences(self):
        assert strip_ansi_codes("\x1b[?25lHidden cursor\x1b[?25h") == "Hidden cursor"
        assert strip_ansi_codes("\x1b[?2026hSync") == "Sync"
        # Synchronized output mode
        assert strip_ansi_codes("\x1b[?2026lText\x1b[?2026h") == "Text"

    def test_strips_osc_sequences(self):
        assert strip_ansi_codes("\x1b]0;My Title\x07Rest") == "Rest"

    def test_real_claude_code_spinner_output(self):
        text = "\x1b[?2026l\x1b[?2026h\n\x1b[6A\x1b[38;2;215;119;87m✶\x1b[1C\x1b[38;2;221;125;93mPerusing…\x1b[39m"
        result = strip_ansi_codes(text)
        assert "✶" in result
        assert "Perusing…" in result
        assert "\x1b[?2026" not in result
        assert "\x1b[6A" not in result

    def test_preserves_plain_text(self):
        assert strip_ansi_codes("Hello, World!") == "Hello, World!"
        assert strip_ansi_codes("Line 1\nLine 2\nLine 3") == "Line 1\nLine 2\nLine 3"

    def test_empty_string(self):
        assert strip_ansi_codes("") == ""

    def test_mixed_content(self):
        text = "Normal \x1b[1mbold\x1b[0m normal \x1b[31mred\x1b[0m end"
        assert strip_ansi_codes(text) == "Normal bold normal red end"


# ---------------------------------------------------------------------------
# clean_terminal_line
# ---------------------------------------------------------------------------


class TestCleanTerminalLine:
    def test_handles_carriage_return(self):
        assert clean_terminal_line("* spin\r/ spin\r- spin").strip() == "- spin"
        assert clean_terminal_line("old\rnew").strip() == "new"

    def test_handles_mixed_ansi_and_cr(self):
        line = "\x1b[38;2;215;119;87m*\x1b[39m\r\x1b[38;2;215;119;87m·\x1b[39m Thinking"
        assert "Thinking" in clean_terminal_line(line)

    def test_removes_control_characters(self):
        assert clean_terminal_line("hello\x00world") == "helloworld"
        # Tab and newline are preserved
        assert clean_terminal_line("hello\tworld") == "hello\tworld"


# ---------------------------------------------------------------------------
# is_spinner_fragment
# ---------------------------------------------------------------------------


class TestIsSpinnerFragment:
    def test_filters_short_garbage(self):
        assert is_spinner_fragment("ddl") is True
        assert is_spinner_fragment("-fa") is True
        assert is_spinner_fragment("ea") is True
        assert is_spinner_fragment("bn") is True
        assert is_spinner_fragment("6") is True

    def test_filters_spinner_chars(self):
        assert is_spinner_fragment("*") is True
        assert is_spinner_fragment("·") is True
        assert is_spinner_fragment("✶") is True
        assert is_spinner_fragment("✻✽") is True

    def test_filters_thinking_messages(self):
        assert is_spinner_fragment("Fiddle-faddling…") is True
        assert is_spinner_fragment("· Fiddle-faddling… (ctrl+c to interrupt)") is True
        assert is_spinner_fragment("thinking)") is True
        assert is_spinner_fragment("ought for 2s)") is True
        assert is_spinner_fragment("thought for 5s)") is True

    def test_keeps_meaningful_content(self):
        assert is_spinner_fragment("⏺Read(.issue-orchestrator/prompts/simple-fix.md)") is False
        assert is_spinner_fragment("⎿ Read 221 lines") is False
        assert is_spinner_fragment("⏺Bash(git status)") is False
        assert is_spinner_fragment("Welcome back Bruce!") is False
        assert is_spinner_fragment("On branch main") is False
        assert is_spinner_fragment("./src/issue_orchestrator/infra/hooks/hooks.py") is False

    def test_keeps_separator_lines(self):
        assert is_spinner_fragment("────────────") is False
        assert is_spinner_fragment("━━━━━━━━━━━━") is False

    def test_keeps_prompts(self):
        assert is_spinner_fragment("❯") is False


# ---------------------------------------------------------------------------
# dedupe_consecutive_lines
# ---------------------------------------------------------------------------


class TestDedupeConsecutiveLines:
    def test_removes_duplicates(self):
        lines = ["line1", "line1", "line1", "line2", "line2", "line3"]
        assert dedupe_consecutive_lines(lines) == ["line1", "line2", "line3"]

    def test_collapses_separators(self):
        lines = ["text", "────────────────", "──────────────────────", "more"]
        result = dedupe_consecutive_lines(lines)
        assert len([l for l in result if l.strip().startswith("─")]) == 1

    def test_empty_input(self):
        assert dedupe_consecutive_lines([]) == []


# ---------------------------------------------------------------------------
# extract_stream_json_text
# ---------------------------------------------------------------------------


class TestExtractStreamJsonText:
    def test_returns_none_for_non_json(self):
        assert extract_stream_json_text(["plain text", "more text"]) is None

    def test_decodes_stream_events(self):
        lines = [
            '{"type":"system","subtype":"init"}',
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}',
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":" world\\nLine 2"}}}',
        ]
        result = extract_stream_json_text(lines)
        assert result == ["Hello world", "Line 2"]


# ---------------------------------------------------------------------------
# Full cleaning pipeline
# ---------------------------------------------------------------------------


class TestFullCleaningPipeline:
    def test_realistic_terminal_garbage(self):
        raw_lines = [
            "\x1b[?25l\x1b[?2004h\x1b[?1004h\x1b[>1u",  # Init sequences
            "\x1b[38;2;215;119;87m· Fiddle-faddling…\x1b[39m",  # Thinking
            "*\r/\r-\r\\",  # Spinner animation
            "\x1b[6A\x1b[2Cddl",  # Cursor movement + fragment
            "⏺Bash(git status)",  # Tool call
            "On branch main",  # Actual output
            "  nothing to commit",  # Actual output
            "\x1b[38;2;215;119;87m✶\x1b[39m Fiddle-faddling…",  # Thinking
            "────────────────────────",  # Separator
            "────────────────────────",  # Dup separator
            "❯",  # Prompt
        ]

        cleaned = []
        for line in raw_lines:
            c = clean_terminal_line(line)
            if c.strip() and not is_spinner_fragment(c):
                cleaned.append(c)
        cleaned = dedupe_consecutive_lines(cleaned)

        content = "\n".join(cleaned)
        assert "Bash(git status)" in content
        assert "On branch main" in content
        assert "nothing to commit" in content
        assert "ddl" not in content
        assert "Fiddle-faddling" not in content
        assert sum(1 for l in cleaned if l.strip().startswith("─")) <= 1


# ---------------------------------------------------------------------------
# CleaningLogWriter
# ---------------------------------------------------------------------------


class TestCleaningLogWriter:
    def test_basic_ansi_stripping(self, tmp_path: Path):
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"\x1b[31mHello World\x1b[0m\n")
        writer.close()
        assert log.read_text().strip() == "Hello World"

    def test_chunked_writes(self, tmp_path: Path):
        """A single line split across multiple write() calls."""
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"Hello ")
        writer.write(b"World\n")
        writer.close()
        assert log.read_text().strip() == "Hello World"

    def test_carriage_return_handling(self, tmp_path: Path):
        """Spinner \r overwrites — only final content kept."""
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"* spinning\r/ spinning\rFinal content\n")
        writer.close()
        assert "Final content" in log.read_text()
        assert "spinning" not in log.read_text()

    def test_spinner_filtering(self, tmp_path: Path):
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"*\n")  # spinner char
        writer.write(b"Fiddle-faddling...\n")  # UI noise (note: the actual check uses unicode …)
        writer.write(b"Real content here\n")
        writer.close()
        content = log.read_text()
        assert "Real content here" in content
        # The spinner char line should be filtered
        lines = [l for l in content.splitlines() if l.strip()]
        assert all("*" != l.strip() for l in lines)

    def test_consecutive_dedup(self, tmp_path: Path):
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"Same line\nSame line\nSame line\nDifferent\n")
        writer.close()
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        assert lines == ["Same line", "Different"]

    def test_close_flushes_buffer(self, tmp_path: Path):
        """Incomplete line in buffer is written on close()."""
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"No newline at end")
        writer.close()
        assert "No newline at end" in log.read_text()

    def test_multiple_lines_in_one_write(self, tmp_path: Path):
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"Line one\nLine two\nLine three\n")
        writer.close()
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        assert lines == ["Line one", "Line two", "Line three"]

    def test_flush_delegates(self, tmp_path: Path):
        """flush() should not raise."""
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        writer.write(b"flush test content\n")
        writer.flush()
        writer.close()
        assert "flush test content" in log.read_text()

    def test_name_attribute(self, tmp_path: Path):
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        assert writer.name == str(log)
        writer.close()

    def test_write_returns_length(self, tmp_path: Path):
        """write() must return the number of bytes received (pexpect contract)."""
        log = tmp_path / "ui-session.log"
        writer = CleaningLogWriter(log)
        data = b"\x1b[31mHello\x1b[0m\n"
        assert writer.write(data) == len(data)
        writer.close()
