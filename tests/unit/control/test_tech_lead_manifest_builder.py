"""Unit tests for TechLeadManifestBuilder."""

from dataclasses import dataclass
from typing import Optional

from issue_orchestrator.control.tech_lead_manifest_builder import (
    TechLeadCandidatePolicy,
    TechLeadManifestBuilder,
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

    def __init__(self, prs: list[MockPR] | None = None):
        self.prs = prs or []
        self.get_prs_with_label_calls: list[tuple[str, str]] = []

    def get_prs_with_label(self, label: str, state: str = "all") -> list[MockPR]:
        self.get_prs_with_label_calls.append((label, state))
        # Return PRs that have the requested label
        return [pr for pr in self.prs if label in pr.labels]


class TestTechLeadManifestBuilder:
    """Tests for TechLeadManifestBuilder."""

    def test_build_empty_when_no_prs(self):
        """Returns empty manifest when no PRs have the code-reviewed label."""
        host = MockRepositoryHost(prs=[])
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

        manifest = builder.build(data_dir="tech-lead-data")

        assert manifest.session_type == "tech_lead"
        assert manifest.data_dir == "tech-lead-data"
        assert manifest.prs == []
        assert len(host.get_prs_with_label_calls) == 1
        assert host.get_prs_with_label_calls[0] == ("code-reviewed", "all")

    def test_build_includes_prs_needing_tech_lead(self):
        """Includes PRs with code-reviewed but not tech-lead-reviewed."""
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
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

        manifest = builder.build(data_dir="session/tech-lead-data")

        assert len(manifest.prs) == 2
        assert manifest.prs[0].number == 1
        assert manifest.prs[0].title == "PR One"
        assert manifest.prs[1].number == 2

    def test_build_excludes_already_triaged(self):
        """Excludes PRs that already have tech-lead-reviewed label."""
        prs = [
            MockPR(
                number=1,
                title="Needs Tech Lead",
                url="https://github.com/org/repo/pull/1",
                branch="branch-1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="Already Triaged",
                url="https://github.com/org/repo/pull/2",
                branch="branch-2",
                labels=["code-reviewed", "tech-lead-reviewed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

        manifest = builder.build(data_dir="data")

        assert len(manifest.prs) == 1
        assert manifest.prs[0].number == 1
        assert manifest.prs[0].title == "Needs Tech Lead"

    def test_build_excludes_tech_lead_failed(self):
        """Excludes PRs that have tech-lead-failed label."""
        prs = [
            MockPR(
                number=1,
                title="Needs Tech Lead",
                url="https://github.com/org/repo/pull/1",
                branch="branch-1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="Tech Lead Failed",
                url="https://github.com/org/repo/pull/2",
                branch="branch-2",
                labels=["code-reviewed", "tech-lead-failed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

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
        builder = TechLeadManifestBuilder(
            host,
            watch_label="my-reviewed",
            candidate_policy=TechLeadCandidatePolicy(
                tech_lead_reviewed_label="my-triaged",
                tech_lead_failed_label="my-failed",
            ),
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
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

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
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

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
                title="Needs Tech Lead",
                url="u1",
                branch="b1",
                labels=["code-reviewed"],
            ),
            MockPR(
                number=2,
                title="Triaged",
                url="u2",
                branch="b2",
                labels=["code-reviewed", "tech-lead-reviewed"],
            ),
            MockPR(
                number=3,
                title="Failed",
                url="u3",
                branch="b3",
                labels=["code-reviewed", "tech-lead-failed"],
            ),
            MockPR(
                number=4,
                title="Both (edge case)",
                url="u4",
                branch="b4",
                labels=["code-reviewed", "tech-lead-reviewed", "tech-lead-failed"],
            ),
            MockPR(
                number=5,
                title="Also Needs Tech Lead",
                url="u5",
                branch="b5",
                labels=["code-reviewed"],
            ),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TechLeadManifestBuilder(host, candidate_policy=TechLeadCandidatePolicy())

        manifest = builder.build(data_dir="data")

        # Only PRs 1 and 5 should be included
        assert len(manifest.prs) == 2
        numbers = [pr.number for pr in manifest.prs]
        assert 1 in numbers
        assert 5 in numbers
        assert 2 not in numbers
        assert 3 not in numbers
        assert 4 not in numbers

    def test_build_honors_filter_label_scope(self):
        """On filtered runs the manifest only audits filter-scoped PRs.

        The shared candidate owner carries the repository filter, so the
        manifest set matches the threshold set the fact gatherer counted.
        """
        prs = [
            MockPR(number=1, title="In scope", url="u1", branch="b1",
                   labels=["code-reviewed", "io:e2e:run-1"]),
            MockPR(number=2, title="Out of scope", url="u2", branch="b2",
                   labels=["code-reviewed"]),
        ]
        host = MockRepositoryHost(prs=prs)
        builder = TechLeadManifestBuilder(
            host,
            candidate_policy=TechLeadCandidatePolicy(required_label="io:e2e:run-1"),
        )

        manifest = builder.build(data_dir="data")

        assert [pr.number for pr in manifest.prs] == [1]


class TestTechLeadCandidatePolicy:
    """The single candidate-eligibility owner shared by facts + manifest (#6768 r5)."""

    def test_candidate_truth_table(self):
        policy = TechLeadCandidatePolicy()
        assert policy.is_candidate(["code-reviewed"]) is True
        assert policy.is_candidate(["code-reviewed", "tech-lead-reviewed"]) is False
        assert policy.is_candidate(["code-reviewed", "tech-lead-failed"]) is False

    def test_required_label_scopes_candidates(self):
        policy = TechLeadCandidatePolicy(required_label="io:e2e:run-1")
        assert policy.is_candidate(["code-reviewed", "io:e2e:run-1"]) is True
        assert policy.is_candidate(["code-reviewed"]) is False

    def test_from_config_honors_custom_labels_and_filter(self):
        from issue_orchestrator.infra.config import Config

        config = Config()
        config.tech_lead_reviewed_label = "my-triaged"
        config.tech_lead_failed_label = "my-failed"
        config.filtering.label = "scope-x"

        policy = TechLeadCandidatePolicy.from_config(config)

        assert policy.is_candidate(["code-reviewed", "scope-x"]) is True
        assert policy.is_candidate(["code-reviewed", "scope-x", "my-triaged"]) is False
        assert policy.is_candidate(["code-reviewed", "scope-x", "my-failed"]) is False
        assert policy.is_candidate(["code-reviewed"]) is False

    def test_from_config_defaults(self):
        from issue_orchestrator.infra.config import Config

        policy = TechLeadCandidatePolicy.from_config(Config())

        assert policy.is_candidate(["code-reviewed"]) is True
        assert policy.is_candidate(["code-reviewed", "tech-lead-reviewed"]) is False
        assert policy.is_candidate(["code-reviewed", "tech-lead-failed"]) is False
