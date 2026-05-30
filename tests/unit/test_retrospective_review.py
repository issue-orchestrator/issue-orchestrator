"""Retrospective review workflow policy tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.control.retrospective_review import (
    build_retrospective_review_existing_work,
    discover_retrospective_review_issues,
    find_orchestrator_pr_for_issue,
    preflight_retrospective_review_issue,
    queue_retrospective_review_request,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    ORCHESTRATOR_PR_MARKER,
    OrchestratorState,
    PendingRetrospectiveReview,
)
from issue_orchestrator.infra.config import Config


class FakeRepositoryHost:
    def __init__(self) -> None:
        self.issues: dict[int, Issue] = {}
        self.prs_by_issue: dict[int, list[SimpleNamespace]] = {}
        self.list_calls: list[dict[str, object]] = []

    def get_issue(self, issue_number: int) -> Issue | None:
        return self.issues.get(issue_number)

    def get_prs_for_issue(
        self,
        issue_number: int,
        state: str = "open",
    ) -> list[SimpleNamespace]:
        assert state == "all"
        return self.prs_by_issue.get(issue_number, [])

    def create_issue_key(self, issue_number: int) -> FakeIssueKey:
        return FakeIssueKey(str(issue_number))

    def list_issues(
        self,
        *,
        labels: list[str],
        state: str,
        limit: int,
    ) -> list[Issue]:
        self.list_calls.append({"labels": labels, "state": state, "limit": limit})
        trigger = labels[0]
        return [
            issue
            for issue in self.issues.values()
            if trigger in issue.labels
        ][:limit]


def make_config() -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.retrospective_review_enabled = True
    config.retrospective_review_trigger_label = "lack-of-review-redo"
    config.retrospective_reviewed_label = "retrospective-reviewed"
    config.retrospective_changes_requested_label = "retrospective-changes-requested"
    config.code_review_agent = "agent:reviewer"
    config.agents = {
        "agent:web": AgentConfig(prompt_path=Path("/tmp/web.md")),
        "agent:reviewer": AgentConfig(prompt_path=Path("/tmp/reviewer.md")),
    }
    return config


def make_issue(
    number: int = 365,
    *,
    labels: list[str] | None = None,
    state: str = "closed",
) -> Issue:
    if labels is None:
        labels = ["agent:web"]
    return Issue(
        number=number,
        title=f"Issue {number}",
        labels=labels,
        state=state,
        repo="owner/repo",
    )


def test_preflight_closed_issue_queues_review_without_reopen_or_filter_gate() -> None:
    config = make_config()
    config.filtering.label = "not-on-this-issue"
    repo = FakeRepositoryHost()
    issue = make_issue(state="closed")
    repo.issues[365] = issue
    repo.prs_by_issue[365] = [
        SimpleNamespace(
            number=512,
            url="https://github.com/owner/repo/pull/512",
            body=f"{ORCHESTRATOR_PR_MARKER}\n",
        )
    ]

    decision = preflight_retrospective_review_issue(365, issue, repo, config)

    assert decision.eligible is True
    assert decision.action == "queue_review"
    assert decision.will_reopen is False
    assert decision.prior_pr_number == 512
    assert decision.reason == (
        "Closed issue will stay closed unless retrospective review requests changes"
    )


def test_preflight_skips_issue_without_agent_label() -> None:
    config = make_config()
    repo = FakeRepositoryHost()
    issue = make_issue(labels=[])

    decision = preflight_retrospective_review_issue(365, issue, repo, config)

    assert decision.eligible is False
    assert decision.action == "skipped"
    assert "no agent:* label" in decision.reason


def test_queue_retrospective_review_request_is_idempotent() -> None:
    config = make_config()
    repo = FakeRepositoryHost()
    issue = make_issue()
    decision = preflight_retrospective_review_issue(365, issue, repo, config)
    state = OrchestratorState()

    assert queue_retrospective_review_request(
        state=state,
        repository_host=repo,
        decision=decision,
    ) is True
    assert queue_retrospective_review_request(
        state=state,
        repository_host=repo,
        decision=decision,
    ) is False
    assert len(state.pending_retrospective_reviews) == 1
    queued = state.pending_retrospective_reviews[0]
    assert queued.issue_number == 365
    assert queued.agent_label == "agent:web"
    assert queued.trigger_label == "lack-of-review-redo"


def test_discover_retrospective_review_issues_scans_trigger_label_across_states() -> None:
    config = make_config()
    repo = FakeRepositoryHost()
    repo.issues[365] = make_issue(labels=["agent:web", "lack-of-review-redo"])
    repo.issues[366] = make_issue(
        366,
        labels=["agent:unknown", "lack-of-review-redo"],
        state="open",
    )
    repo.issues[367] = make_issue(367, labels=["agent:web"])

    discovered = discover_retrospective_review_issues(
        repository_host=repo,
        config=config,
        already_issue_numbers=set(),
    )

    assert [item.issue_number for item in discovered] == [365]
    assert repo.list_calls == [
        {
            "labels": ["lack-of-review-redo"],
            "state": "all",
            "limit": config.filtering.fetch_limit,
        }
    ]


def test_find_orchestrator_pr_for_issue_requires_signature() -> None:
    repo = FakeRepositoryHost()
    repo.prs_by_issue[365] = [
        SimpleNamespace(
            number=511,
            url="https://github.com/owner/repo/pull/511",
            body="manual PR",
        ),
        SimpleNamespace(
            number=512,
            url="https://github.com/owner/repo/pull/512",
            body=f"{ORCHESTRATOR_PR_MARKER}\n",
        ),
    ]

    assert find_orchestrator_pr_for_issue(repo, 365) == (
        512,
        "https://github.com/owner/repo/pull/512",
    )


def test_retrospective_review_existing_work_makes_already_done_explicit() -> None:
    review = PendingRetrospectiveReview(
        issue_key=FakeIssueKey("365"),
        issue_number=365,
        issue_title="Already finished",
        agent_label="agent:web",
        trigger_label="lack-of-review-redo",
    )

    prompt = build_retrospective_review_existing_work(review)

    assert "RETROSPECTIVE REVIEW MODE" in prompt
    assert "may already be closed or say the work is done" in prompt
    assert "not a reason to stop" in prompt
    assert "Do not modify code" in prompt
