"""Unit tests for TechLeadDownloader."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


@dataclass
class CommandResult:
    """Mock command result."""

    returncode: int
    stdout: str
    stderr: str


class MockRepositoryHost:
    """Mock RepositoryHost for testing."""

    def __init__(self, prs: dict[int, MockPR] | None = None):
        self._prs = prs or {}
        self.get_pr_calls: list[int] = []

    def get_pr(self, pr_number: int) -> Optional[MockPR]:
        self.get_pr_calls.append(pr_number)
        return self._prs.get(pr_number)


class MockCommandRunner:
    """Mock CommandRunner for testing."""

    def __init__(self, results: dict[str, CommandResult] | None = None):
        self._results = results or {}
        self.run_calls: list[list[str]] = []

    def run(self, args: list[str]) -> CommandResult:
        self.run_calls.append(args)
        # Match by PR number in args
        for arg in args:
            if arg in self._results:
                return self._results[arg]
        # Default success result
        return CommandResult(returncode=0, stdout="", stderr="")


class TestTechLeadDownloader:
    """Tests for TechLeadDownloader."""

    def test_download_empty_manifest(self, tmp_path: Path):
        """Handles empty manifest gracefully."""
        host = MockRepositoryHost()
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(data_dir="tech-lead-data", prs=[])
        result = downloader.download(manifest, tmp_path)

        assert result.prs == []
        assert len(host.get_pr_calls) == 0
        assert len(runner.run_calls) == 0

    def test_download_requires_data_dir(self, tmp_path: Path):
        """Raises error if data_dir not set."""
        host = MockRepositoryHost()
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(data_dir="", prs=[
            PRToReview(number=1, title="PR", url="u", branch="b"),
        ])

        try:
            downloader.download(manifest, tmp_path)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "data_dir" in str(e)

    def test_download_creates_diff_file(self, tmp_path: Path):
        """Downloads and writes diff for each PR."""
        host = MockRepositoryHost(prs={
            42: MockPR(
                number=42,
                title="Test PR",
                url="https://github.com/org/repo/pull/42",
                branch="test",
                labels=[],
            ),
        })
        runner = MockCommandRunner(results={
            "42": CommandResult(
                returncode=0,
                stdout="diff --git a/file.py b/file.py\n+added line",
                stderr="",
            ),
        })
        downloader = TechLeadDownloader(host, runner)

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
        host = MockRepositoryHost(prs={
            42: MockPR(
                number=42,
                title="Test PR",
                url="https://github.com/org/repo/pull/42",
                branch="test-branch",
                labels=["bug", "priority"],
                body="PR description here",
                state="open",
            ),
        })
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

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

    def test_download_handles_diff_error(self, tmp_path: Path):
        """Writes error message when diff fetch fails."""
        host = MockRepositoryHost(prs={
            99: MockPR(number=99, title="PR", url="u", branch="b", labels=[]),
        })
        runner = MockCommandRunner(results={
            "99": CommandResult(
                returncode=1,
                stdout="",
                stderr="gh: PR not found",
            ),
        })
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=99, title="PR", url="u", branch="b")],
        )
        downloader.download(manifest, tmp_path)

        diff_path = tmp_path / "data" / "pr-99-diff.txt"
        assert diff_path.exists()
        content = diff_path.read_text()
        assert "Error fetching diff" in content
        assert "PR not found" in content

    def test_download_handles_missing_pr(self, tmp_path: Path):
        """Writes error metadata when PR not found."""
        host = MockRepositoryHost(prs={})  # No PRs
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

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
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[]),
            2: MockPR(number=2, title="PR 2", url="u2", branch="b2", labels=[]),
            3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[]),
        })
        runner = MockCommandRunner(results={
            "1": CommandResult(0, "diff1", ""),
            "2": CommandResult(0, "diff2", ""),
            "3": CommandResult(0, "diff3", ""),
        })
        downloader = TechLeadDownloader(host, runner)

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

    def test_download_continues_on_pr_failure(self, tmp_path: Path):
        """Continues downloading other PRs even if one fails."""
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[]),
            # PR 2 missing
            3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[]),
        })
        runner = MockCommandRunner(results={
            "1": CommandResult(0, "diff1", ""),
            "2": CommandResult(1, "", "not found"),
            "3": CommandResult(0, "diff3", ""),
        })
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[
                PRToReview(number=1, title="PR 1", url="u1", branch="b1"),
                PRToReview(number=2, title="PR 2", url="u2", branch="b2"),
                PRToReview(number=3, title="PR 3", url="u3", branch="b3"),
            ],
        )
        downloader.download(manifest, tmp_path)

        # PR 1 and 3 should have proper files
        assert (tmp_path / "data" / "pr-1-diff.txt").exists()
        assert (tmp_path / "data" / "pr-3-diff.txt").exists()
        assert "diff1" in (tmp_path / "data" / "pr-1-diff.txt").read_text()
        assert "diff3" in (tmp_path / "data" / "pr-3-diff.txt").read_text()

    def test_download_creates_data_directory(self, tmp_path: Path):
        """Creates data directory if it doesn't exist."""
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR", url="u", branch="b", labels=[]),
        })
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="deep/nested/tech-lead-data",
            prs=[PRToReview(number=1, title="PR", url="u", branch="b")],
        )
        downloader.download(manifest, tmp_path)

        assert (tmp_path / "deep" / "nested" / "tech-lead-data").exists()
        assert (tmp_path / "deep" / "nested" / "tech-lead-data" / "pr-1-diff.txt").exists()

    def test_download_calls_gh_pr_diff(self, tmp_path: Path):
        """Calls gh pr diff with correct arguments."""
        host = MockRepositoryHost(prs={
            42: MockPR(number=42, title="PR", url="u", branch="b", labels=[]),
        })
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=42, title="PR", url="u", branch="b")],
        )
        downloader.download(manifest, tmp_path)

        assert len(runner.run_calls) == 1
        assert runner.run_calls[0] == ["gh", "pr", "diff", "42"]
