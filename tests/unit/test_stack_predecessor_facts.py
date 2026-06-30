"""Tests for the git/PR stack-predecessor facts provider (ADR-0029, #6595).

These verify the production provider projects a predecessor's PR / label state
into PredecessorFacts the dependency gate report consumes, and that every
missing-signal / error path is fail-safe (conservative all-False facts) so a
dependent stack slice is never unblocked on an unverified base.

Review state (``code-reviewed`` / ``needs-rework``) is **PR-scoped** — the
completion and review-exchange paths add it to the PR, not the issue — so these
tests assert the provider reads it from ``PRInfo.labels``. Blocking and
``validation-failed`` state is issue-scoped and read from the issue labels.
"""

from pathlib import Path

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.dependencies import DependencyTarget
from issue_orchestrator.execution.stack_predecessor_facts import (
    GitStackPredecessorFactsProvider,
)
from issue_orchestrator.infra.config import AgentConfig, Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def _pr(number: int, branch: str, state: str = "open", labels=None) -> PRInfo:
    return PRInfo(
        number=number,
        title=f"PR {number}",
        url=f"https://example/pr/{number}",
        branch=branch,
        body="",
        state=state,
        labels=labels or [],
    )


class FakeRepoHost:
    """Minimal RepositoryHost double for branch/PR/label reads.

    By default it honors the ``state`` filter. ``ignore_state_filter=True``
    reproduces the production GitHubAdapter behavior (see F2): the broad
    association search returns PRs in any state regardless of the requested
    ``state``, so the provider must defend itself.
    """

    def __init__(self, *, ignore_state_filter: bool = False):
        self.prs_by_issue: dict[int, list[PRInfo]] = {}
        self.labels_by_issue: dict[int, list[str]] = {}
        self.raise_on: set[int] = set()
        self._ignore_state_filter = ignore_state_filter

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        if issue_number in self.raise_on:
            raise RuntimeError("transport error")
        prs = self.prs_by_issue.get(issue_number, [])
        if self._ignore_state_filter or state == "all":
            return list(prs)
        return [pr for pr in prs if pr.state == state]

    def get_issue_labels(self, issue_number: int) -> list[str]:
        if issue_number in self.raise_on:
            raise RuntimeError("transport error")
        return self.labels_by_issue.get(issue_number, [])


@pytest.fixture
def config():
    return Config(
        repo="owner/repo",
        repo_root=Path("/tmp/repo"),
        worktree_base=Path("/tmp"),
        agents={"claude": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
    )


@pytest.fixture
def label_manager(config):
    return LabelManager(config)


@pytest.fixture
def repo():
    return FakeRepoHost()


@pytest.fixture
def provider(repo, label_manager):
    return GitStackPredecessorFactsProvider(
        repository_host=repo, label_manager=label_manager, repo="owner/repo"
    )


def _make_provider(repo, label_manager) -> GitStackPredecessorFactsProvider:
    return GitStackPredecessorFactsProvider(
        repository_host=repo, label_manager=label_manager, repo="owner/repo"
    )


class TestGather:
    def test_open_pr_validated_and_reviewed_is_work_ready(self, provider, repo):
        # code-reviewed lives on the PR (PR-scoped review state), not the issue.
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=["code-reviewed"])]
        repo.labels_by_issue[20] = []

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is True
        assert facts.branch_name == "20-base"
        assert facts.validation_passed is True
        assert facts.agent_reviewed is True
        assert facts.merged is False

    def test_code_reviewed_only_on_pr_unblocks_review_fact(self, provider, repo):
        # PR carries the review label; the issue has none -> still reviewed.
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=["code-reviewed"])]
        repo.labels_by_issue[20] = []

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.agent_reviewed is True

    def test_code_reviewed_only_on_issue_does_not_unblock_review_fact(
        self, provider, repo
    ):
        # Review state is PR-scoped: a stray issue label must NOT count as review.
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=[])]
        repo.labels_by_issue[20] = ["code-reviewed"]

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is True
        assert facts.agent_reviewed is False

    def test_open_pr_not_reviewed_blocks_review_fact(self, provider, repo):
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=[])]  # no code-reviewed
        repo.labels_by_issue[20] = []

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is True
        assert facts.agent_reviewed is False

    def test_needs_rework_on_pr_blocks_review_fact(self, provider, repo):
        # A PR in rework is not cleanly reviewed even if code-reviewed lingers.
        repo.prs_by_issue[20] = [
            _pr(101, "20-base", labels=["code-reviewed", "needs-rework"])
        ]
        repo.labels_by_issue[20] = []

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is True
        assert facts.agent_reviewed is False

    def test_validation_failed_label_blocks_validation_fact(self, provider, repo):
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=["code-reviewed"])]
        repo.labels_by_issue[20] = ["validation-failed"]

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.validation_passed is False

    def test_blocking_issue_label_blocks_validation_fact(self, provider, repo):
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=["code-reviewed"])]
        repo.labels_by_issue[20] = ["blocked"]

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.validation_passed is False

    def test_blocking_pr_label_blocks_validation_fact(self, provider, repo):
        # A blocking label on the PR itself is also respected.
        repo.prs_by_issue[20] = [
            _pr(101, "20-base", labels=["code-reviewed", "blocked"])
        ]
        repo.labels_by_issue[20] = []

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.validation_passed is False

    def test_no_open_pr_is_conservative(self, provider, repo):
        # Only a closed (unmerged) PR exists -> no usable open branch.
        repo.prs_by_issue[20] = [_pr(101, "20-base", state="closed")]

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is False
        assert facts.validation_passed is False
        assert facts.agent_reviewed is False

    def test_read_error_is_fail_safe(self, provider, repo):
        repo.raise_on.add(20)

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is False
        assert facts.validation_passed is False
        assert facts.agent_reviewed is False

    def test_cross_repo_predecessor_is_conservative(self, provider, repo):
        target = DependencyTarget(20, repository="other/repo")
        # Even if local host had data, a cross-repo target stays blocked.
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=["code-reviewed"])]

        facts = provider.gather_facts([target])[target]

        assert facts.branch_usable is False
        assert facts.validation_passed is False

    def test_gathers_each_requested_target(self, provider, repo):
        repo.prs_by_issue[20] = [_pr(101, "20-base", labels=["code-reviewed"])]
        repo.prs_by_issue[30] = []  # no PR

        result = provider.gather_facts([DependencyTarget(20), DependencyTarget(30)])

        assert result[DependencyTarget(20)].branch_usable is True
        assert result[DependencyTarget(30)].branch_usable is False


class TestPRCandidateValidation:
    """F2: the provider validates the PR candidate itself, never trusting the
    broad association lookup or the (unenforced) host-side state filter."""

    def test_closed_pr_returned_before_open_pr_is_not_used_as_base(
        self, label_manager
    ):
        # Host ignores `state="open"` and returns the closed PR first.
        repo = FakeRepoHost(ignore_state_filter=True)
        repo.prs_by_issue[20] = [
            _pr(101, "20-old", state="closed", labels=["code-reviewed"]),
            _pr(102, "20-base", state="open", labels=["code-reviewed"]),
        ]
        provider = _make_provider(repo, label_manager)

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        # The closed PR must be skipped; the open, branch-matching PR is the base.
        assert facts.branch_usable is True
        assert facts.branch_name == "20-base"

    def test_only_closed_pr_stays_blocked_even_when_state_ignored(
        self, label_manager
    ):
        repo = FakeRepoHost(ignore_state_filter=True)
        repo.prs_by_issue[20] = [
            _pr(101, "20-base", state="closed", labels=["code-reviewed"])
        ]
        provider = _make_provider(repo, label_manager)

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is False
        assert facts.validation_passed is False
        assert facts.agent_reviewed is False

    def test_pr_with_non_predecessor_branch_does_not_unblock(self, label_manager):
        # An open PR associated by a title mention but whose branch belongs to a
        # different issue must not be treated as the predecessor's stack base.
        repo = FakeRepoHost()
        repo.prs_by_issue[20] = [
            _pr(101, "55-unrelated", state="open", labels=["code-reviewed"])
        ]
        provider = _make_provider(repo, label_manager)

        facts = provider.gather_facts([DependencyTarget(20)])[DependencyTarget(20)]

        assert facts.branch_usable is False
        assert facts.validation_passed is False
        assert facts.agent_reviewed is False
