"""Unit tests for TechLeadDownloader."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from issue_orchestrator.execution.tech_lead_downloader import TechLeadDownloader
from issue_orchestrator.domain.tech_lead_manifest import (
    TechLeadManifest,
    PRToReview,
)


@dataclass
class MockPR:
    """Mock PR object for testing."""

    number: int
    title: str
    url: str
    branch: str
    labels: list[str]
    body: Optional[str] = None
    state: str = "open"


class MockRepositoryHost:
    """Mock RepositoryHost for testing."""

    def __init__(
        self,
        prs: dict[int, MockPR] | None = None,
        *,
        diffs: dict[int, str] | None = None,
        diff_errors: dict[int, Exception] | None = None,
    ):
        self._prs = prs or {}
        self._diffs = diffs or {}
        self._diff_errors = diff_errors or {}
        self.get_pr_calls: list[int] = []
        self.get_pr_diff_calls: list[int] = []

    def get_pr(self, pr_number: int) -> Optional[MockPR]:
        self.get_pr_calls.append(pr_number)
        return self._prs.get(pr_number)

    def get_pr_diff(self, pr_number: int) -> str:
        self.get_pr_diff_calls.append(pr_number)
        if error := self._diff_errors.get(pr_number):
            raise error
        return self._diffs.get(pr_number, f"diff for #{pr_number}")


class TestTechLeadDownloader:
    """Tests for TechLeadDownloader."""

    def test_download_empty_manifest(self, tmp_path: Path):
        """Handles empty manifest gracefully."""
        host = MockRepositoryHost()
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(data_dir="tech-lead-data", prs=[])
        result = downloader.download(manifest, tmp_path)

        assert result.prs == []
        assert len(host.get_pr_calls) == 0
        assert len(host.get_pr_diff_calls) == 0

    def test_download_requires_data_dir(self, tmp_path: Path):
        """Raises error if data_dir not set."""
        host = MockRepositoryHost()
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="",
            prs=[
                PRToReview(number=1, title="PR", url="u", branch="b"),
            ],
        )

        try:
            downloader.download(manifest, tmp_path)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "data_dir" in str(e)

    def test_download_creates_diff_file(self, tmp_path: Path):
        """Downloads and writes diff for each PR."""
        host = MockRepositoryHost(
            prs={
                42: MockPR(
                    number=42,
                    title="Test PR",
                    url="https://github.com/org/repo/pull/42",
                    branch="test",
                    labels=[],
                ),
            },
            diffs={42: "diff --git a/file.py b/file.py\n+added line"},
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="tech-lead-data",
            prs=[PRToReview(number=42, title="Test PR", url="u", branch="b")],
        )
        result = downloader.download(manifest, tmp_path)

        # Check diff file was created
        diff_path = tmp_path / "tech-lead-data" / "pr-42-diff.txt"
        assert diff_path.exists()
        assert "diff --git" in diff_path.read_text()

        # Check manifest was updated
        assert result.prs[0].files.diff == "pr-42-diff.txt"

    def test_download_creates_metadata_file(self, tmp_path: Path):
        """Downloads and writes metadata for each PR."""
        host = MockRepositoryHost(
            prs={
                42: MockPR(
                    number=42,
                    title="Test PR",
                    url="https://github.com/org/repo/pull/42",
                    branch="test-branch",
                    labels=["bug", "priority"],
                    body="PR description here",
                    state="open",
                ),
            }
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="tech-lead-data",
            prs=[PRToReview(number=42, title="Test PR", url="u", branch="b")],
        )
        result = downloader.download(manifest, tmp_path)

        # Check metadata file was created
        meta_path = tmp_path / "tech-lead-data" / "pr-42-meta.json"
        assert meta_path.exists()

        metadata = json.loads(meta_path.read_text())
        assert metadata["number"] == 42
        assert metadata["title"] == "Test PR"
        assert metadata["body"] == "PR description here"
        assert metadata["branch"] == "test-branch"
        assert "bug" in metadata["labels"]
        assert metadata["state"] == "open"

        # Check manifest was updated
        assert result.prs[0].files.metadata == "pr-42-meta.json"

    def test_download_fails_loudly_when_diff_fetch_fails(self, tmp_path: Path):
        """Missing review evidence cannot masquerade as a readable diff file."""
        host = MockRepositoryHost(
            prs={99: MockPR(number=99, title="PR", url="u", branch="b", labels=[])},
            diff_errors={99: RuntimeError("PR diff unavailable")},
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=99, title="PR", url="u", branch="b")],
        )
        with pytest.raises(RuntimeError, match="PR diff unavailable"):
            downloader.download(manifest, tmp_path)

        diff_path = tmp_path / "data" / "pr-99-diff.txt"
        assert not diff_path.exists()
        assert not (tmp_path / "data" / "pr-99-meta.json").exists()

    def test_download_handles_missing_pr(self, tmp_path: Path):
        """Writes error metadata when PR not found."""
        host = MockRepositoryHost(prs={})  # No PRs
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=999, title="Missing", url="u", branch="b")],
        )
        downloader.download(manifest, tmp_path)

        meta_path = tmp_path / "data" / "pr-999-meta.json"
        assert meta_path.exists()
        metadata = json.loads(meta_path.read_text())
        assert "error" in metadata
        assert "999" in metadata["error"]

    def test_download_multiple_prs(self, tmp_path: Path):
        """Downloads data for multiple PRs."""
        host = MockRepositoryHost(
            prs={
                1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[]),
                2: MockPR(number=2, title="PR 2", url="u2", branch="b2", labels=[]),
                3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[]),
            },
            diffs={1: "diff1", 2: "diff2", 3: "diff3"},
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[
                PRToReview(number=1, title="PR 1", url="u1", branch="b1"),
                PRToReview(number=2, title="PR 2", url="u2", branch="b2"),
                PRToReview(number=3, title="PR 3", url="u3", branch="b3"),
            ],
        )
        result = downloader.download(manifest, tmp_path)

        # Check all files created
        assert (tmp_path / "data" / "pr-1-diff.txt").exists()
        assert (tmp_path / "data" / "pr-2-diff.txt").exists()
        assert (tmp_path / "data" / "pr-3-diff.txt").exists()
        assert (tmp_path / "data" / "pr-1-meta.json").exists()
        assert (tmp_path / "data" / "pr-2-meta.json").exists()
        assert (tmp_path / "data" / "pr-3-meta.json").exists()

        # Check manifest updated
        assert result.prs[0].files.diff == "pr-1-diff.txt"
        assert result.prs[1].files.diff == "pr-2-diff.txt"
        assert result.prs[2].files.diff == "pr-3-diff.txt"

    def test_download_stops_before_later_prs_after_diff_failure(self, tmp_path: Path):
        """A partial manifest is rejected instead of launching a partial review."""
        host = MockRepositoryHost(
            prs={
                1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[]),
                2: MockPR(number=2, title="PR 2", url="u2", branch="b2", labels=[]),
                3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[]),
            },
            diffs={1: "diff1", 3: "diff3"},
            diff_errors={2: RuntimeError("not found")},
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[
                PRToReview(number=1, title="PR 1", url="u1", branch="b1"),
                PRToReview(number=2, title="PR 2", url="u2", branch="b2"),
                PRToReview(number=3, title="PR 3", url="u3", branch="b3"),
            ],
        )
        with pytest.raises(RuntimeError, match="not found"):
            downloader.download(manifest, tmp_path)

        # PR 1 completed before the failure; PR 3 was never attempted.
        assert (tmp_path / "data" / "pr-1-diff.txt").exists()
        assert "diff1" in (tmp_path / "data" / "pr-1-diff.txt").read_text()
        assert not (tmp_path / "data" / "pr-3-diff.txt").exists()
        assert host.get_pr_diff_calls == [1, 2]

    def test_download_creates_data_directory(self, tmp_path: Path):
        """Creates data directory if it doesn't exist."""
        host = MockRepositoryHost(
            prs={
                1: MockPR(number=1, title="PR", url="u", branch="b", labels=[]),
            }
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="deep/nested/tech-lead-data",
            prs=[PRToReview(number=1, title="PR", url="u", branch="b")],
        )
        downloader.download(manifest, tmp_path)

        assert (tmp_path / "deep" / "nested" / "tech-lead-data").exists()
        assert (
            tmp_path / "deep" / "nested" / "tech-lead-data" / "pr-1-diff.txt"
        ).exists()

    def test_download_reads_diff_through_repository_host(self, tmp_path: Path):
        """The manifest fetch uses the authenticated repository boundary."""
        host = MockRepositoryHost(
            prs={
                42: MockPR(number=42, title="PR", url="u", branch="b", labels=[]),
            }
        )
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=42, title="PR", url="u", branch="b")],
        )
        downloader.download(manifest, tmp_path)

        assert host.get_pr_diff_calls == [42]
