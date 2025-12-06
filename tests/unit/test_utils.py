"""Unit tests for utility functions."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from issue_orchestrator.utils import slugify, ensure_directory_exists, format_list_for_display


class TestSlugify:
    """Test the slugify function."""

    def test_basic_slugification(self):
        """Test basic text conversion to slug."""
        assert slugify("Hello World") == "hello-world"

    def test_lowercase_conversion(self):
        """Test that text is converted to lowercase."""
        assert slugify("HELLO WORLD") == "hello-world"
        assert slugify("MiXeD CaSe") == "mixed-case"

    def test_special_characters_removal(self):
        """Test that special characters are removed/replaced."""
        assert slugify("Fix: Bug #123") == "fix-bug-123"
        assert slugify("User@domain.com") == "user-domain-com"

    def test_multiple_spaces_to_single_hyphen(self):
        """Test that multiple spaces become single hyphens."""
        assert slugify("Hello   World") == "hello-world"
        assert slugify("Test\t\tTab") == "test-tab"

    def test_issue_title_slugification(self):
        """Test slugification of GitHub issue titles."""
        assert slugify("[TEST] Simple backend task") == "test-simple-backend-task"
        assert slugify("[FEATURE] User authentication") == "feature-user-authentication"

    def test_with_numbers(self):
        """Test slugification with numbers."""
        assert slugify("Issue #255 - Test task") == "issue-255-test-task"
        assert slugify("Version 2.0 release") == "version-2-0-release"

    def test_max_length(self):
        """Test max_length parameter."""
        long_text = "This is a very long title that should be truncated at max length"
        result = slugify(long_text, max_length=20)
        assert len(result) <= 20
        assert result == "this-is-a-very-long"

    def test_no_leading_trailing_hyphens(self):
        """Test that slugs don't have leading or trailing hyphens."""
        assert slugify("   Hello   ") == "hello"
        assert slugify("@@@Hello@@@") == "hello"

    def test_consecutive_hyphens_collapsed(self):
        """Test that consecutive hyphens are collapsed."""
        assert slugify("Hello---World") == "hello-world"
        assert slugify("Test---Multiple---Hyphens") == "test-multiple-hyphens"

    def test_empty_string(self):
        """Test slugification of empty string."""
        assert slugify("") == ""

    def test_only_special_characters(self):
        """Test slugification of only special characters."""
        assert slugify("@#$%^&*") == ""
        assert slugify("!!!???") == ""

    def test_unicode_characters(self):
        """Test slugification with unicode characters."""
        assert slugify("Café") == "café"
        assert slugify("Naïve approach") == "naïve-approach"

    def test_hyphens_in_input(self):
        """Test that existing hyphens are preserved."""
        assert slugify("hello-world") == "hello-world"
        assert slugify("kebab-case-text") == "kebab-case-text"


class TestEnsureDirectoryExists:
    """Test the ensure_directory_exists function."""

    def test_creates_directory(self):
        """Test that directory is created if it doesn't exist."""
        with TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir) / "test_dir"
            assert not test_path.exists()
            ensure_directory_exists(test_path)
            assert test_path.exists()
            assert test_path.is_dir()

    def test_creates_nested_directories(self):
        """Test that nested directories are created."""
        with TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir) / "level1" / "level2" / "level3"
            assert not test_path.exists()
            ensure_directory_exists(test_path)
            assert test_path.exists()
            assert test_path.is_dir()

    def test_idempotent(self):
        """Test that function is idempotent."""
        with TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir) / "test_dir"
            ensure_directory_exists(test_path)
            assert test_path.exists()
            # Call again should not raise
            ensure_directory_exists(test_path)
            assert test_path.exists()

    def test_existing_directory(self):
        """Test that function doesn't fail for existing directories."""
        with TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir)
            # tmpdir already exists
            ensure_directory_exists(test_path)
            assert test_path.exists()


class TestFormatListForDisplay:
    """Test the format_list_for_display function."""

    def test_single_item(self):
        """Test formatting a single item."""
        assert format_list_for_display(["apple"]) == "apple"

    def test_multiple_items_default_separator(self):
        """Test formatting multiple items with default separator."""
        assert format_list_for_display(["a", "b", "c"]) == "a, b, c"

    def test_multiple_items_custom_separator(self):
        """Test formatting with custom separator."""
        assert format_list_for_display(["a", "b", "c"], separator=" | ") == "a | b | c"

    def test_empty_list(self):
        """Test formatting empty list."""
        assert format_list_for_display([]) == ""

    def test_custom_separator_semicolon(self):
        """Test formatting with semicolon separator."""
        assert format_list_for_display(["x", "y", "z"], separator="; ") == "x; y; z"

    def test_custom_separator_newline(self):
        """Test formatting with newline separator."""
        result = format_list_for_display(["line1", "line2"], separator="\n")
        assert result == "line1\nline2"
