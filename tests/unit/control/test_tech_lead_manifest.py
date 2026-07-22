"""Unit tests for TechLeadManifest and related dataclasses."""

import json
from pathlib import Path

from issue_orchestrator.domain.tech_lead_manifest import (
    PRFiles,
    PRToReview,
    TechLeadManifest,
)


class TestPRFiles:
    """Tests for PRFiles dataclass."""

    def test_default_values(self):
        """PRFiles defaults to empty strings."""
        files = PRFiles()
        assert files.diff == ""
        assert files.metadata == ""

    def test_with_values(self):
        """PRFiles stores provided values."""
        files = PRFiles(diff="pr-1-diff.txt", metadata="pr-1-meta.json")
        assert files.diff == "pr-1-diff.txt"
        assert files.metadata == "pr-1-meta.json"


class TestPRToReview:
    """Tests for PRToReview dataclass."""

    def test_minimal_creation(self):
        """PRToReview can be created with minimal required fields."""
        pr = PRToReview(
            number=123,
            title="Test PR",
            url="https://github.com/org/repo/pull/123",
            branch="feature-branch",
        )
        assert pr.number == 123
        assert pr.title == "Test PR"
        assert pr.url == "https://github.com/org/repo/pull/123"
        assert pr.branch == "feature-branch"
        assert isinstance(pr.files, PRFiles)

    def test_with_files(self):
        """PRToReview stores files reference."""
        files = PRFiles(diff="pr-99-diff.txt", metadata="pr-99-meta.json")
        pr = PRToReview(
            number=99,
            title="Full PR",
            url="https://github.com/org/repo/pull/99",
            branch="full-branch",
            files=files,
        )
        assert pr.number == 99
        assert pr.files.diff == "pr-99-diff.txt"
        assert pr.files.metadata == "pr-99-meta.json"


class TestTechLeadManifest:
    """Tests for TechLeadManifest."""

    def test_default_values(self):
        """TechLeadManifest defaults correctly."""
        manifest = TechLeadManifest()
        assert manifest.session_type == "tech_lead"
        assert manifest.generated_at == ""
        assert manifest.data_dir == ""
        assert manifest.prs == []

    def test_to_dict_empty(self):
        """to_dict works with empty manifest."""
        manifest = TechLeadManifest(data_dir=".issue-orchestrator/sessions/run1/tech-lead-data")
        result = manifest.to_dict()

        assert result["session_type"] == "tech_lead"
        assert result["data_dir"] == ".issue-orchestrator/sessions/run1/tech-lead-data"
        assert result["prs"] == []
        # generated_at gets filled in if empty
        assert "generated_at" in result
        assert result["generated_at"] != ""

    def test_to_dict_with_prs(self):
        """to_dict serializes PRs correctly."""
        pr = PRToReview(
            number=42,
            title="Test",
            url="https://github.com/org/repo/pull/42",
            branch="test-branch",
            files=PRFiles(diff="pr-42-diff.txt", metadata="pr-42-meta.json"),
        )
        manifest = TechLeadManifest(
            generated_at="2025-01-21T12:00:00Z",
            data_dir="tech-lead-data",
            prs=[pr],
        )
        result = manifest.to_dict()

        assert len(result["prs"]) == 1
        pr_dict = result["prs"][0]
        assert pr_dict["number"] == 42
        assert pr_dict["title"] == "Test"
        assert pr_dict["files"]["diff"] == "pr-42-diff.txt"
        assert pr_dict["files"]["metadata"] == "pr-42-meta.json"

    def test_from_dict_empty(self):
        """from_dict handles empty manifest."""
        data = {
            "session_type": "tech_lead",
            "generated_at": "2025-01-21T12:00:00Z",
            "data_dir": "some-dir",
            "prs": [],
        }
        manifest = TechLeadManifest.from_dict(data)

        assert manifest.session_type == "tech_lead"
        assert manifest.generated_at == "2025-01-21T12:00:00Z"
        assert manifest.data_dir == "some-dir"
        assert manifest.prs == []

    def test_from_dict_with_prs(self):
        """from_dict deserializes PRs correctly."""
        data = {
            "session_type": "tech_lead",
            "generated_at": "2025-01-21T12:00:00Z",
            "data_dir": "tech-lead-data",
            "prs": [
                {
                    "number": 99,
                    "title": "Big PR",
                    "url": "https://github.com/org/repo/pull/99",
                    "branch": "big-branch",
                    "files": {
                        "diff": "pr-99-diff.txt",
                        "metadata": "pr-99-meta.json",
                    },
                }
            ],
        }
        manifest = TechLeadManifest.from_dict(data)

        assert len(manifest.prs) == 1
        pr = manifest.prs[0]
        assert pr.number == 99
        assert pr.title == "Big PR"
        assert pr.files.diff == "pr-99-diff.txt"

    def test_from_dict_handles_missing_optional_fields(self):
        """from_dict uses defaults for missing optional fields."""
        data = {
            "prs": [
                {
                    "number": 1,
                    "title": "Minimal PR",
                    "url": "https://github.com/org/repo/pull/1",
                    "branch": "minimal",
                    # No files
                }
            ],
        }
        manifest = TechLeadManifest.from_dict(data)

        pr = manifest.prs[0]
        assert pr.files.diff == ""
        assert pr.files.metadata == ""

    def test_roundtrip(self):
        """to_dict and from_dict are inverse operations."""
        original = TechLeadManifest(
            generated_at="2025-01-21T15:00:00Z",
            data_dir="test-data",
            prs=[
                PRToReview(
                    number=1,
                    title="PR 1",
                    url="https://github.com/org/repo/pull/1",
                    branch="branch-1",
                    files=PRFiles(diff="pr-1-diff.txt", metadata="pr-1-meta.json"),
                ),
                PRToReview(
                    number=2,
                    title="PR 2",
                    url="https://github.com/org/repo/pull/2",
                    branch="branch-2",
                ),
            ],
        )

        # Round-trip through dict
        as_dict = original.to_dict()
        restored = TechLeadManifest.from_dict(as_dict)

        assert restored.session_type == original.session_type
        assert restored.generated_at == original.generated_at
        assert restored.data_dir == original.data_dir
        assert len(restored.prs) == len(original.prs)

        for orig_pr, rest_pr in zip(original.prs, restored.prs):
            assert rest_pr.number == orig_pr.number
            assert rest_pr.title == orig_pr.title
            assert rest_pr.files.diff == orig_pr.files.diff

    def test_write_creates_file(self, tmp_path: Path):
        """write() creates the manifest file."""
        manifest = TechLeadManifest(
            generated_at="2025-01-21T12:00:00Z",
            data_dir="tech-lead-data",
            prs=[
                PRToReview(
                    number=42,
                    title="Test PR",
                    url="https://github.com/org/repo/pull/42",
                    branch="test",
                )
            ],
        )

        manifest_path = tmp_path / "tech-lead-data" / "manifest.json"
        manifest.write(manifest_path)

        assert manifest_path.exists()
        content = json.loads(manifest_path.read_text())
        assert content["session_type"] == "tech_lead"
        assert len(content["prs"]) == 1
        assert content["prs"][0]["number"] == 42

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        """write() creates parent directories if needed."""
        manifest = TechLeadManifest()
        deep_path = tmp_path / "a" / "b" / "c" / "manifest.json"

        manifest.write(deep_path)

        assert deep_path.exists()

    def test_read_loads_file(self, tmp_path: Path):
        """read() loads manifest from file."""
        manifest_data = {
            "session_type": "tech_lead",
            "generated_at": "2025-01-21T12:00:00Z",
            "data_dir": "data",
            "prs": [
                {
                    "number": 77,
                    "title": "From File",
                    "url": "https://github.com/org/repo/pull/77",
                    "branch": "from-file",
                    "files": {"diff": "d.txt", "metadata": "m.json"},
                }
            ],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_data))

        manifest = TechLeadManifest.read(manifest_path)

        assert manifest.session_type == "tech_lead"
        assert manifest.generated_at == "2025-01-21T12:00:00Z"
        assert len(manifest.prs) == 1
        assert manifest.prs[0].number == 77
        assert manifest.prs[0].files.diff == "d.txt"

    def test_write_then_read_roundtrip(self, tmp_path: Path):
        """write then read preserves data."""
        original = TechLeadManifest(
            generated_at="2025-01-21T16:00:00Z",
            data_dir="session-data",
            prs=[
                PRToReview(
                    number=123,
                    title="Roundtrip",
                    url="https://github.com/org/repo/pull/123",
                    branch="roundtrip-branch",
                    files=PRFiles(diff="diff.txt", metadata="meta.json"),
                )
            ],
        )

        path = tmp_path / "manifest.json"
        original.write(path)
        loaded = TechLeadManifest.read(path)

        assert loaded.generated_at == original.generated_at
        assert loaded.data_dir == original.data_dir
        assert len(loaded.prs) == 1
        assert loaded.prs[0].number == 123
        assert loaded.prs[0].files.diff == "diff.txt"

    def test_get_pr_numbers_empty(self):
        """get_pr_numbers returns empty list for empty manifest."""
        manifest = TechLeadManifest()
        assert manifest.get_pr_numbers() == []

    def test_get_pr_numbers_with_prs(self):
        """get_pr_numbers returns all PR numbers."""
        manifest = TechLeadManifest(
            prs=[
                PRToReview(number=1, title="A", url="u1", branch="b1"),
                PRToReview(number=5, title="B", url="u5", branch="b5"),
                PRToReview(number=99, title="C", url="u99", branch="b99"),
            ]
        )
        assert manifest.get_pr_numbers() == [1, 5, 99]
