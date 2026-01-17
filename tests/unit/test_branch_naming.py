"""Unit tests for branch naming utilities."""

from issue_orchestrator.domain.branch_naming import (
    slugify,
    generate_branch_name,
    extract_issue_number_from_branch,
)


class TestSlugify:
    """Test the slugify function."""

    def test_basic_conversion_to_lowercase(self):
        """Test basic text conversion to lowercase."""
        assert slugify("Hello World") == "hello-world"

    def test_removes_special_characters(self):
        """Test that special characters are removed."""
        assert slugify("Fix: Bug #123!") == "fix-bug-123"

    def test_converts_spaces_and_underscores_to_hyphens(self):
        """Test that spaces and underscores are converted to hyphens."""
        assert slugify("hello_world test") == "hello-world-test"

    def test_removes_consecutive_hyphens(self):
        """Test that consecutive hyphens are collapsed into single hyphen."""
        assert slugify("hello---world") == "hello-world"

    def test_strips_leading_trailing_hyphens(self):
        """Test that leading and trailing hyphens are removed."""
        assert slugify("---hello-world---") == "hello-world"

    def test_max_length_default(self):
        """Test that default max_length is respected."""
        long_text = "a" * 50
        result = slugify(long_text)
        assert len(result) <= 40
        assert result == "a" * 40

    def test_max_length_custom(self):
        """Test that custom max_length is respected."""
        long_text = "hello-world-" * 5
        result = slugify(long_text, max_length=20)
        assert len(result) <= 20

    def test_max_length_removes_trailing_hyphens(self):
        """Test that truncation removes trailing hyphens."""
        text = "hello-world-" * 5
        result = slugify(text, max_length=15)
        assert result.endswith("-") is False
        assert len(result) <= 15

    def test_unicode_normalization(self):
        """Test that unicode characters are normalized and transliterated."""
        assert slugify("café") == "cafe"
        assert slugify("naïve") == "naive"
        assert slugify("François") == "francois"

    def test_empty_string(self):
        """Test that empty string returns empty string."""
        assert slugify("") == ""

    def test_only_special_characters(self):
        """Test string with only special characters becomes empty."""
        assert slugify("!@#$%^&*()") == ""

    def test_numbers_preserved(self):
        """Test that numbers are preserved."""
        assert slugify("Issue 123 Test") == "issue-123-test"

    def test_mixed_case_converted(self):
        """Test that mixed case is converted to lowercase."""
        assert slugify("MyTestBranch") == "mytestbranch"

    def test_hyphens_in_input_preserved(self):
        """Test that hyphens in input are preserved (as separators)."""
        assert slugify("my-test-branch") == "my-test-branch"


class TestGenerateBranchName:
    """Test the generate_branch_name function."""

    def test_basic_generation(self):
        """Test basic branch name generation."""
        result = generate_branch_name(123, "Fix the bug")
        assert result == "123-fix-the-bug"

    def test_includes_issue_number_at_start(self):
        """Test that issue number is at the start."""
        result = generate_branch_name(456, "Some title")
        assert result.startswith("456-")

    def test_slugifies_title(self):
        """Test that title is slugified."""
        result = generate_branch_name(789, "Add New Feature!")
        assert result == "789-add-new-feature"

    def test_long_title_truncated(self):
        """Test that long titles are truncated."""
        long_title = "a" * 60
        result = generate_branch_name(123, long_title)
        # Should use max_length=50 for the slug
        assert len(result) <= 64  # 3 digits + hyphen + 50 + potential hyphen removal

    def test_with_special_characters(self):
        """Test generation with special characters in title."""
        result = generate_branch_name(100, "Fix: Issue #100 (urgent!)")
        assert result == "100-fix-issue-100-urgent"

    def test_with_unicode(self):
        """Test generation with unicode characters."""
        result = generate_branch_name(200, "Café Menu Update")
        assert result == "200-cafe-menu-update"

    def test_with_large_issue_number(self):
        """Test generation with large issue number."""
        result = generate_branch_name(999999, "Test")
        assert result.startswith("999999-")


class TestExtractIssueNumberFromBranch:
    """Test the extract_issue_number_from_branch function."""

    def test_basic_extraction(self):
        """Test basic issue number extraction."""
        result = extract_issue_number_from_branch("123-fix-the-bug")
        assert result == 123

    def test_single_digit(self):
        """Test extraction of single digit issue number."""
        result = extract_issue_number_from_branch("1-test")
        assert result == 1

    def test_large_number(self):
        """Test extraction of large issue number."""
        result = extract_issue_number_from_branch("999999-test")
        assert result == 999999

    def test_no_hyphen_after_number_not_matched(self):
        """Test that number without hyphen is not matched."""
        result = extract_issue_number_from_branch("123test")
        assert result is None

    def test_number_not_at_start(self):
        """Test that number not at start is not matched."""
        result = extract_issue_number_from_branch("fix-123-bug")
        assert result is None

    def test_no_number(self):
        """Test that branch with no leading number returns None."""
        result = extract_issue_number_from_branch("fix-the-bug")
        assert result is None

    def test_empty_string(self):
        """Test that empty string returns None."""
        result = extract_issue_number_from_branch("")
        assert result is None

    def test_just_number_with_hyphen(self):
        """Test branch that is just number and hyphen."""
        result = extract_issue_number_from_branch("123-")
        assert result == 123

    def test_generated_branch_name_roundtrip(self):
        """Test that extracted number matches the original issue number."""
        original_issue = 456
        branch_name = generate_branch_name(original_issue, "Some title")
        extracted = extract_issue_number_from_branch(branch_name)
        assert extracted == original_issue

    def test_multiple_roundtrips(self):
        """Test roundtrip extraction with various issue numbers."""
        test_cases = [1, 42, 123, 999, 10000, 999999]
        for issue_num in test_cases:
            branch_name = generate_branch_name(issue_num, "Test")
            extracted = extract_issue_number_from_branch(branch_name)
            assert extracted == issue_num, f"Failed for issue {issue_num}"
