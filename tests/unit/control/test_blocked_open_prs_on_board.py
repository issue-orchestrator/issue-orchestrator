"""A blocked issue's open PR reaches the tech-lead board snapshot (#7294).

porchpin 2026-09-23: an exchange halt labelled issues ``blocked-failed``, io
published their validated work as green PRs anyway, and review validity then
dropped every review with ``reason=issue_blocked``. The board snapshot's
``blocked_issues`` came from dependency problems only, so no health review saw
the PRs. These tests drive the real scanner, the workflow that folds its scans
into state, and the snapshot builder -- with GitHub faked at the port.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.pr_scanner import PRScanner
from issue_orchestrator.domain.blocked_open_pr import (
    BlockedPRLane,
    BlockedPRSkipReason,
)
from issue_orchestrator.domain.board_snapshot import (
    BoardSnapshot,
    BoardTechLeadWriteHealth,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.builders import IssueBuilder
from tests.conftest import MockEventSink, MockGitHubAdapter

NOW = datetime(2026, 9, 23, 6, 48)


class CountingGitHub(MockGitHubAdapter):
    """Counts every read the scan makes, so the test can bound them."""

    def __init__(self) -> None:
        super().__init__()
        self.label_listings: list[str] = []
        self.issue_reads: list[int] = []

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        self.label_listings.append(label)
        return super().get_prs_with_label(label, state)

    def get_issue(self, issue_number: int):
        self.issue_reads.append(issue_number)
        return super().get_issue(issue_number)


@pytest.fixture
def config() -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = "needs-code-review"
    return config


@pytest.fixture
def github() -> CountingGitHub:
    return CountingGitHub()


def _issue(github: CountingGitHub, number: int, *labels: str) -> None:
    github.issues.append(
        IssueBuilder()
        .with_number(number)
        .with_title(f"Issue {number}")
        .with_labels("agent:developer", *labels)
        .build()
    )


def _pr(
    github: CountingGitHub,
    number: int,
    issue: int,
    *labels: str,
    branch: str | None = None,
    draft: bool | None = False,
) -> None:
    head = branch or f"{issue}-work"
    github.prs.setdefault(head, []).append(
        PRInfo(
            number=number,
            title=f"PR {number}",
            url=f"https://github.com/owner/repo/pull/{number}",
            branch=head,
            body=f"Closes #{issue}",
            state="open",
            labels=list(labels),
            draft=draft,
        )
    )


def _workflow(config: Config, github: CountingGitHub) -> GitHubWorkflow:
    scanner = PRScanner(config=config, repository=github, events=MockEventSink())
    return GitHubWorkflow(
        config, MagicMock(), MagicMock(), MagicMock(), scanner, None, EventContext()
    )


def _snapshot(state: OrchestratorState) -> BoardSnapshot:
    return BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: [],
        log_tail_provider=lambda n: [],
        case_file_reader=lambda: (),
        shipped_fix_reader=lambda limit: (),
        e2e_health_reader=lambda now: None,
        tech_lead_write_health_reader=lambda now: BoardTechLeadWriteHealth(
            verdict="idle", is_alarm=False, reason="test", stale_after_hours=48.0
        ),
        session_activity_reader=lambda session: None,
        clock=lambda: NOW,
    ).build(state)


def test_a_green_pr_of_a_blocked_failed_issue_is_on_the_board_with_its_skip_count(
    config: Config, github: CountingGitHub
) -> None:
    _issue(github, 320, "blocked-failed", "pr-pending")
    _pr(github, 376, 320, "needs-code-review", draft=True)
    workflow = _workflow(config, github)
    state = OrchestratorState()

    for _ in range(3):
        workflow.scan_needs_code_review_prs(state, issue_branches={})

    assert state.discovered_reviews == []
    snapshot = _snapshot(state)
    assert snapshot.blocked_issues == []
    (entry,) = snapshot.blocked_open_prs or []
    assert (entry.issue_number, entry.issue_title, entry.pr_number) == (
        320,
        "Issue 320",
        376,
    )
    assert entry.draft is True
    assert entry.lane == "review"
    assert entry.skip_reason == "issue_blocked"
    assert entry.blocking_labels == ["blocked-failed"]
    assert entry.skip_count == 3
    assert entry.pr_url == "https://github.com/owner/repo/pull/376"
    # The field reaches the file the tech lead reads, and reads back.
    written = snapshot.to_dict()["blocked_open_prs"]
    assert written is not None and written[0]["skip_count"] == 3
    assert BoardSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_the_board_costs_no_github_read_beyond_the_scan_it_already_ran(
    config: Config, github: CountingGitHub
) -> None:
    for issue, pr in ((320, 376), (353, 378), (364, 379)):
        _issue(github, issue, "blocked-failed")
        _pr(github, pr, issue, "needs-code-review")
    state = OrchestratorState()

    _workflow(config, github).scan_needs_code_review_prs(state, issue_branches={})
    reads = (list(github.label_listings), list(github.issue_reads))
    snapshot = _snapshot(state)

    assert reads == (["needs-code-review"], [320, 353, 364])
    assert (github.label_listings, github.issue_reads) == reads
    assert [e.pr_number for e in snapshot.blocked_open_prs or []] == [376, 378, 379]


def test_unblocking_the_issue_takes_the_pr_off_the_board(
    config: Config, github: CountingGitHub
) -> None:
    _issue(github, 320, "blocked-failed")
    _pr(github, 376, 320, "needs-code-review")
    workflow = _workflow(config, github)
    state = OrchestratorState()
    workflow.scan_needs_code_review_prs(state, issue_branches={})

    github.issues[0].labels.remove("blocked-failed")
    workflow.scan_needs_code_review_prs(state, issue_branches={})

    assert _snapshot(state).blocked_open_prs == []
    assert [r.pr_number for r in state.discovered_reviews] == [376]


def test_a_blocking_label_on_the_pr_itself_is_reported_as_pr_blocked(
    config: Config, github: CountingGitHub
) -> None:
    _issue(github, 320)
    _pr(github, 376, 320, "needs-code-review", "needs-human")

    scan = PRScanner(config, github, MockEventSink()).scan_for_reviews(
        [], [], issue_branches={}
    )

    (observation,) = scan.blocked
    assert observation.skip_reason is BlockedPRSkipReason.PR_BLOCKED
    assert observation.blocking_labels == ("needs-human",)


def test_a_review_skipped_for_a_non_blocking_reason_is_not_reported(
    config: Config, github: CountingGitHub
) -> None:
    _issue(github, 320, "needs-rework")
    _pr(github, 376, 320, "needs-code-review")

    scan = PRScanner(config, github, MockEventSink()).scan_for_reviews(
        [], [], issue_branches={}
    )

    assert scan.reviews == [] and scan.blocked == []


def test_a_rework_pr_of_a_blocked_issue_is_on_the_board(
    config: Config, github: CountingGitHub
) -> None:
    _issue(github, 353, "needs-human")
    _pr(github, 378, 353, "needs-rework")
    workflow = _workflow(config, github)
    state = OrchestratorState()

    workflow.scan_needs_rework_prs(state, issue_branches={})
    workflow.scan_needs_rework_prs(state, issue_branches={})

    assert state.discovered_reworks == []
    (entry,) = _snapshot(state).blocked_open_prs or []
    assert (entry.lane, entry.skip_reason, entry.skip_count) == (
        BlockedPRLane.REWORK.value,
        "issue_blocked",
        2,
    )
    assert entry.blocking_labels == ["needs-human"]


def test_a_blocked_rework_pr_skips_the_issue_read_it_never_needed(
    config: Config, github: CountingGitHub
) -> None:
    _issue(github, 353)
    _pr(github, 378, 353, "needs-rework", "blocked-failed")

    scan = PRScanner(config, github, MockEventSink()).scan_for_reworks(
        [], [], issue_branches={}
    )

    (observation,) = scan.blocked
    assert observation.skip_reason is BlockedPRSkipReason.PR_BLOCKED
    assert github.issue_reads == []
    assert observation.issue_title is None


@pytest.mark.parametrize("lane", ["review", "rework"])
def test_a_prior_attempt_pr_is_not_reported(
    config: Config, github: CountingGitHub, lane: str
) -> None:
    _issue(github, 320, "blocked-failed")
    label = "needs-code-review" if lane == "review" else "needs-rework"
    _pr(github, 376, 320, label, branch="320-old-attempt")
    scanner = PRScanner(config, github, MockEventSink())
    branches = {320: "320-current-attempt"}

    scan = (
        scanner.scan_for_reviews([], [], issue_branches=branches)
        if lane == "review"
        else scanner.scan_for_reworks([], [], issue_branches=branches)
    )

    assert scan.blocked == []


def test_a_pr_level_block_stays_on_the_board_after_the_issue_is_cleared(
    config: Config, github: CountingGitHub
) -> None:
    """Retry clears the ISSUE's labels; a label on the PR still holds it.

    The prompt tells the tech lead that a ``pr_blocked`` entry needs the PR's
    label removed, not an issue retry; this is the behaviour that claim rests on.
    """
    _issue(github, 320, "blocked-failed")
    _pr(github, 376, 320, "needs-code-review", "blocked-failed")
    workflow = _workflow(config, github)
    state = OrchestratorState()
    workflow.scan_needs_code_review_prs(state, issue_branches={})

    github.issues[0].labels.remove("blocked-failed")
    workflow.scan_needs_code_review_prs(state, issue_branches={})

    (entry,) = _snapshot(state).blocked_open_prs or []
    assert entry.skip_reason == BlockedPRSkipReason.PR_BLOCKED.value
    assert state.discovered_reviews == []
