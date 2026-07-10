"""Unit tests for TriageManifestBuilder."""

from dataclasses import dataclass
from typing import Optional

from issue_orchestrator.control.triage_manifest_builder import TriageManifestBuilder


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

    def __init__(self, prs: list[MockPR] | None = None):
        self.prs = prs or []
        self.get_prs_with_label_calls: list[tuple[str, str]] = []

    def get_prs_with_label(self, label: str, state: str = "all") -> list[MockPR]:
        self.get_prs_with_label_calls.append((label, state))
        # Return PRs that have the requested label
        return [pr for pr in self.prs if label in pr.labels]


class TestTriageManifestBuilder:
    """Tests for TriageManifestBuilder."""

    def test_build_empty_when_no_prs(self):
        """Returns empty manifest when no PRs have the code-reviewed label."""
        host = MockRepositoryHost(prs=[])
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="triage-data")

        assert manifest.session_type == "triage"
        assert manifest.data_dir == "triage-data"
        assert manifest.prs == []
        assert len(host.get_prs_with_label_calls) == 1
        assert host.get_prs_with_label_calls[0] == ("code-reviewed", "all")

    def test_build_includes_prs_needing_triage(self):
        """Includes PRs with code-reviewed but not triage-reviewed."""
        prs = [
            MockPR(
                number=1,
                title="PR One",
                url="https://github.com/org/repo/pull/1",
                branch="branch-1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="PR Two",
                url="https://github.com/org/repo/pull/2",
                branch="branch-2",
                labels=["code-reviewed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="session/triage-data")

        assert len(manifest.prs) == 2
        assert manifest.prs[0].number == 1
        assert manifest.prs[0].title == "PR One"
        assert manifest.prs[1].number == 2

    def test_build_excludes_already_triaged(self):
        """Excludes PRs that already have triage-reviewed label."""
        prs = [
            MockPR(
                number=1,
                title="Needs Triage",
                url="https://github.com/org/repo/pull/1",
                branch="branch-1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="Already Triaged",
                url="https://github.com/org/repo/pull/2",
                branch="branch-2",
                labels=["code-reviewed", "triage-reviewed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="data")

        assert len(manifest.prs) == 1
        assert manifest.prs[0].number == 1
        assert manifest.prs[0].title == "Needs Triage"

    def test_build_excludes_triage_failed(self):
        """Excludes PRs that have triage-failed label."""
        prs = [
            MockPR(
                number=1,
                title="Needs Triage",
                url="https://github.com/org/repo/pull/1",
                branch="branch-1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="Triage Failed",
                url="https://github.com/org/repo/pull/2",
                branch="branch-2",
                labels=["code-reviewed", "triage-failed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="data")

        assert len(manifest.prs) == 1
        assert manifest.prs[0].number == 1

    def test_build_uses_custom_labels(self):
        """Respects custom label configuration."""
        prs = [
            MockPR(
                number=1,
                title="Custom Labels",
                url="https://github.com/org/repo/pull/1",
                branch="branch-1",
                labels=["my-reviewed"],
            ),
            MockPR(
                number=2,
                title="Already Done",
                url="https://github.com/org/repo/pull/2",
                branch="branch-2",
                labels=["my-reviewed", "my-triaged"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TriageManifestBuilder(
            host,
            watch_label="my-reviewed",
            triage_reviewed_label="my-triaged",
            triage_failed_label="my-failed",
        )

        manifest = builder.build(data_dir="data")

        # Should query with custom label
        assert host.get_prs_with_label_calls[0] == ("my-reviewed", "all")
        # Should exclude PR with my-triaged
        assert len(manifest.prs) == 1
        assert manifest.prs[0].number == 1

    def test_build_sets_generated_at(self):
        """Sets generated_at timestamp."""
        host = MockRepositoryHost(prs=[])
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="data")

        assert manifest.generated_at != ""
        # Should be ISO format
        assert "T" in manifest.generated_at
        assert "Z" in manifest.generated_at

    def test_build_initializes_pr_fields(self):
        """PRToReview objects have correct initial values."""
        prs = [
            MockPR(
                number=42,
                title="Test PR",
                url="https://github.com/org/repo/pull/42",
                branch="test-branch",
                labels=["code-reviewed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="data")

        pr = manifest.prs[0]
        assert pr.number == 42
        assert pr.title == "Test PR"
        assert pr.url == "https://github.com/org/repo/pull/42"
        assert pr.branch == "test-branch"
        # Files not populated until downloader runs
        assert pr.files.diff == ""
        assert pr.files.metadata == ""

    def test_build_excludes_both_triaged_and_failed(self):
        """Correctly filters when PRs have mixed labels."""
        prs = [
            MockPR(
                number=1,
                title="Needs Triage",
                url="u1",
                branch="b1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="Triaged",
                url="u2",
                branch="b2",
                labels=["code-reviewed", "triage-reviewed"],
            ),
            MockPR(
                number=3,
                title="Failed",
                url="u3",
                branch="b3",
                labels=["code-reviewed", "triage-failed"],
            ),
            MockPR(
                number=4,
                title="Both (edge case)",
                url="u4",
                branch="b4",
                labels=["code-reviewed", "triage-reviewed", "triage-failed"],
            ),
            MockPR(
                number=5,
                title="Also Needs Triage",
                url="u5",
                branch="b5",
                labels=["code-reviewed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TriageManifestBuilder(host)

        manifest = builder.build(data_dir="data")

        # Only PRs 1 and 5 should be included
        assert len(manifest.prs) == 2
        numbers = [pr.number for pr in manifest.prs]
        assert 1 in numbers
        assert 5 in numbers
        assert 2 not in numbers
        assert 3 not in numbers
        assert 4 not in numbers
