"""No reset path may destroy a PR of published validated work (#7293).

porchpin#392: a tech-lead ``reset_retry`` (wired ``from_scratch=True``) closed a
CI-green PR that validated-work recovery had published, and deleted its branch.
``require_reset`` did not stop it because the RECOVERED record is resolved.

Every reset funnels through ``reset_and_retry_issue`` (dashboard
``/api/reset-retry`` with and without ``from_scratch``, and the tech-lead
executor's production wiring), and that pipeline's destructive primitive is
``maintenance.reset_issue``. Both ask the one lifecycle owner. These tests drive
each path with the real owner and a real lifecycle, plus a control case in
which the PR is closed (the operator's explicit abandonment) and the reset runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.control.actions import ActionResultType, ResetRetryIssueAction
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.maintenance import ResetResult, reset_issue
from issue_orchestrator.control.published_review_custody import (
    PublishedValidatedWorkHeld,
)
from issue_orchestrator.control.tech_lead_reset_retry import STALE_DOWNGRADE_MODE
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.domain.validated_work import ValidatedWorkState
from issue_orchestrator.entrypoints.tech_lead_reset_retry_wiring import (
    build_tech_lead_reset_retry_executor,
)
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.infra.config import Config

from tests.runtime_lifecycle_helpers import runtime_owners
from tests.unit.control.published_review_support import (
    DispositionStore,
    PullRequests,
    custody,
    disposition,
    pr,
)
from tests.unit.test_web import create_mock_orchestrator

ISSUE = 392
PR_NUMBER = 500
STALE = "published_validated_work_under_review"


def _custody(pr_state: str = "open"):
    store = DispositionStore(
        {ISSUE: (disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=PR_NUMBER),)}
    )
    return custody(store, PullRequests({ISSUE: [pr(ISSUE, PR_NUMBER, state=pr_state)]}))


def _live_session():
    return SimpleNamespace(terminal_id=f"issue-{ISSUE}", issue=SimpleNamespace(number=ISSUE))


def _dashboard(pr_state: str):
    mock_orch = create_mock_orchestrator()
    lm = LabelManager(mock_orch.config)
    session_manager = MagicMock()
    mock_orch.deps.session_manager = session_manager
    mock_orch.deps.label_manager = lm
    mock_orch.deps.action_applier = MagicMock()
    mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
    mock_orch.deps.events = MagicMock()
    mock_orch.repository_host.get_issue_labels.return_value = [
        "agent:web", lm.blocked_failed, lm.pr_pending,
    ]
    mock_orch.repository_host.get_issue.return_value = Issue(
        number=ISSUE, title="t", labels=["agent:web", lm.reset_retry_pending],
        state="open", repo="owner/repo",
    )
    mock_orch.state.active_sessions = [_live_session()]
    mock_orch.deps.runtime_lifecycle = runtime_owners(
        session_manager=session_manager,
        active_sessions=mock_orch.state.active_sessions,
        published_review=_custody(pr_state),
    )
    set_orchestrator(mock_orch)
    return mock_orch, session_manager


@pytest.mark.parametrize("from_scratch", [False, True], ids=["reset", "reset-from-scratch"])
def test_dashboard_reset_refuses_before_terminating_anything(from_scratch):
    """Both dashboard resets delete the PR's branch; scratch also supersedes it."""
    mock_orch, session_manager = _dashboard("open")

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
        response = TestClient(app).post(
            "/api/reset-retry", json={"issues": [ISSUE], "from_scratch": from_scratch}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reset"] == []
    (failure,) = payload["failed"]
    assert failure["stale_reason"] == STALE
    assert failure["published_review"]["holds"][0]["pr_number"] == PR_NUMBER
    assert f"close PR #{PR_NUMBER}" in failure["error"]
    reset_issue_mock.assert_not_called()
    session_manager.stop.assert_not_called()
    assert [s.terminal_id for s in mock_orch.state.active_sessions] == [f"issue-{ISSUE}"]
    mock_orch.deps.action_applier.apply.assert_not_called()


def test_dashboard_reset_proceeds_once_the_operator_closed_the_pr():
    mock_orch, _ = _dashboard("closed")

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
        reset_issue_mock.return_value = ResetResult(success=True, issue_number=ISSUE)
        response = TestClient(app).post("/api/reset-retry", json={"issues": [ISSUE]})

    assert response.json()["failed"] == []
    reset_issue_mock.assert_called_once()


def test_the_reset_primitive_refuses_on_its_own():
    """Defense in depth: ``reset_issue`` is the destructive primitive every path calls."""
    applier = MagicMock()
    applier.runtime_lifecycle = runtime_owners(published_review=_custody("open"))
    worktree_manager, working_copy = MagicMock(), MagicMock()

    with pytest.raises(PublishedValidatedWorkHeld):
        reset_issue(
            issue_number=ISSUE,
            config=Config(),
            worktree_manager=worktree_manager,
            working_copy=working_copy,
            action_applier=applier,
            label_manager=LabelManager(Config()),
            current_labels=["blocked-failed"],
            session_history=[],
            completed_today=[],
            from_scratch=True,
            repository_host=MagicMock(),
        )

    assert worktree_manager.method_calls == []
    assert working_copy.method_calls == []
    applier.apply.assert_not_called()


def _tech_lead_executor(pr_state: str):
    lm = LabelManager(Config())
    state = OrchestratorState()
    repository_host = MagicMock()
    repository_host.get_issue.return_value = Issue(
        number=ISSUE, title="t", labels=["agent:web", lm.blocked_failed, lm.pr_pending],
        state="open", repo="owner/repo",
    )
    action_applier = MagicMock()
    deps = SimpleNamespace(
        label_manager=lm,
        events=MagicMock(),
        repository_host=repository_host,
        queue_cache_store=MagicMock(),
        action_applier=action_applier,
        runtime_lifecycle=runtime_owners(
            active_sessions=state.active_sessions, published_review=_custody(pr_state)
        ),
    )
    orchestrator = SimpleNamespace(
        deps=deps, config=Config(), state=state, repository_host=repository_host
    )
    return build_tech_lead_reset_retry_executor(orchestrator), action_applier


def test_tech_lead_reset_retry_downgrades_instead_of_closing_the_pr():
    """The porchpin#392 path, through the production executor wiring."""
    executor, action_applier = _tech_lead_executor("open")

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
        result = executor.apply(
            ResetRetryIssueAction(
                issue_number=ISSUE, rationale="stuck", proposal_id="A1",
                finding_ids=("T1",), anchor_issue_number=ISSUE,
            )
        )

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    assert result.details["boundary"]["stale_reason"] == STALE
    assert result.details["boundary"]["published_review"]["holds"][0]["pr_number"] == PR_NUMBER
    reset_issue_mock.assert_not_called()
    action_applier.apply.assert_not_called()
