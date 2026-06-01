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
    retrospective_review_preflight_payload,
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
        self.search_pr_refs_calls: list[int] = []

    def get_issue(self, issue_number: int) -> Issue | None:
        return self.issues.get(issue_number)

    def search_pr_refs_for_issue(self, issue_number: int) -> list[SimpleNamespace]:
        self.search_pr_refs_calls.append(issue_number)
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
    assert decision.prior_pr_number == 512
    assert decision.reason == (
        "Closed issue will stay closed unless retrospective review requests changes"
    )


def test_preflight_payload_includes_all_decision_fields() -> None:
    config = make_config()
    repo = FakeRepositoryHost()
    issue = make_issue(
        labels=["agent:web", "lack-of-review-redo", "blocked-failed"],
        state="closed",
    )
    repo.issues[365] = issue
    repo.prs_by_issue[365] = [
        SimpleNamespace(
            number=512,
            url="https://github.com/owner/repo/pull/512",
            body=f"{ORCHESTRATOR_PR_MARKER}\n",
        )
    ]

    decision = preflight_retrospective_review_issue(365, issue, repo, config)
    payload = retrospective_review_preflight_payload(
        [decision],
        trigger_label=config.retrospective_review_trigger_label,
    )

    assert payload == {
        "decisions": [
            {
                "issue": 365,
                "title": "Issue 365",
                "state": "closed",
                "labels": ["agent:web", "lack-of-review-redo", "blocked-failed"],
                "eligible": True,
                "action": "queue_review",
                "reason": (
                    "Closed issue will stay closed unless retrospective review "
                    "requests changes"
                ),
                "agent_label": "agent:web",
                "trigger_label": "lack-of-review-redo",
                "prior_pr_number": 512,
                "prior_pr_url": "https://github.com/owner/repo/pull/512",
            }
        ],
        "eligible": [365],
        "skipped": [],
        "workflow": "retrospective_review",
        "trigger_label": "lack-of-review-redo",
    }


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


def test_queue_retrospective_review_request_preserves_all_pending_fields() -> None:
    config = make_config()
    repo = FakeRepositoryHost()
    issue = make_issue(labels=["agent:web", "lack-of-review-redo", "blocked-failed"])
    repo.issues[365] = issue
    repo.prs_by_issue[365] = [
        SimpleNamespace(
            number=512,
            url="https://github.com/owner/repo/pull/512",
            body=f"{ORCHESTRATOR_PR_MARKER}\n",
        )
    ]
    decision = preflight_retrospective_review_issue(365, issue, repo, config)
    state = OrchestratorState()

    assert queue_retrospective_review_request(
        state=state,
        repository_host=repo,
        decision=decision,
    ) is True

    assert len(state.pending_retrospective_reviews) == 1
    queued = state.pending_retrospective_reviews[0]
    assert queued.issue_key == FakeIssueKey("365")
    assert queued.issue_number == 365
    assert queued.issue_title == "Issue 365"
    assert queued.agent_label == "agent:web"
    assert queued.trigger_label == "lack-of-review-redo"
    assert queued.prior_pr_number == 512
    assert queued.prior_pr_url == "https://github.com/owner/repo/pull/512"
    # Real issue labels are captured so the launched session can clear blocking
    # labels at completion (the session otherwise only knows synthetic labels).
    assert queued.issue_labels == ("agent:web", "lack-of-review-redo", "blocked-failed")


def test_discover_retrospective_review_issues_scans_trigger_label_across_states() -> None:
    config = make_config()
    repo = FakeRepositoryHost()
    repo.issues[365] = make_issue(labels=["agent:web", "lack-of-review-redo"])
    repo.prs_by_issue[365] = [
        SimpleNamespace(
            number=512,
            url="https://github.com/owner/repo/pull/512",
            body=f"{ORCHESTRATOR_PR_MARKER}\n",
        )
    ]
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
    assert discovered[0].issue_title == "Issue 365"
    assert discovered[0].agent_label == "agent:web"
    assert discovered[0].trigger_label == "lack-of-review-redo"
    assert discovered[0].issue_key == "365"
    assert discovered[0].issue_labels == ("agent:web", "lack-of-review-redo")
    # Discovery does NOT resolve the prior orchestrator PR — that is an optional
    # prompt hint resolved lazily at launch. Discovery cost must stay O(1) GitHub
    # calls (one label list), independent of issue/PR count, so it can run on
    # every startup recovery and per-tick scan without fanning out.
    assert discovered[0].prior_pr_number is None
    assert discovered[0].prior_pr_url is None
    assert repo.search_pr_refs_calls == []
    assert repo.list_calls == [
        {
            "labels": ["lack-of-review-redo"],
            "state": "all",
            "limit": config.filtering.fetch_limit,
        }
    ]


def test_discover_retrospective_review_issues_github_call_budget_is_constant() -> None:
    """Startup-timing regression guard.

    Discovery once resolved the prior PR per issue by searching for and
    hydrating every candidate PR, so its GitHub-call count was
    O(issues x PRs-per-issue) — which made startup recovery (and the per-tick
    scan) take ~30s for a repo with a backlog of trigger-labeled issues. This
    pins the cost to a single label query, independent of how many issues match
    and how many PRs each one has. We assert on call counts (deterministic),
    not wall-clock (flaky), because call count is the actual regression signal.
    """
    config = make_config()
    repo = FakeRepositoryHost()
    issue_count, prs_per_issue = 20, 10
    for n in range(400, 400 + issue_count):
        repo.issues[n] = make_issue(n, labels=["agent:web", "lack-of-review-redo"])
        repo.prs_by_issue[n] = [
            SimpleNamespace(
                number=1000 + n * 10 + k,
                url=f"https://github.com/owner/repo/pull/{1000 + n * 10 + k}",
                body=f"{ORCHESTRATOR_PR_MARKER}\n" if k == 0 else "manual",
            )
            for k in range(prs_per_issue)
        ]

    discovered = discover_retrospective_review_issues(
        repository_host=repo,
        config=config,
        already_issue_numbers=set(),
    )

    assert len(discovered) == issue_count
    # One label list, regardless of the 20 issues x 10 PRs = 200 PRs present.
    assert len(repo.list_calls) == 1
    assert repo.search_pr_refs_calls == []


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
