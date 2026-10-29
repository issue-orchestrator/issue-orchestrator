"""Queued work on an external PR keeps the authority it was queued under (#9499 r4 F5).

Discovery records the complete authority (effort, exact PR, issue and agent)
an integration effort admitted a review or a rework under; the planner's
queue actions and the pending items carry it, and it survives a restart, so
the launch judges the work against it unchanged however the PR's labels or
base change meanwhile.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.actions import QueueReviewAction, QueueReworkAction
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.external_pr_adoption import LaunchAuthority
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import DiscoveredReview, DiscoveredRework, PendingReview, PendingRework
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.execution.pending_work_codec import decode_claim, encode_claim
from issue_orchestrator.infra.config import Config

KEY = GitHubIssueKey(repo="owner/repo", external_id="231")
AUTHORITY = LaunchAuthority(effort="integration", issue_number=231, pr_number=321, agent="agent:backend")
REWORK_AUTHORITY = LaunchAuthority(effort="integration", issue_number=232, pr_number=322, agent="agent:frontend")


def _review(authority: LaunchAuthority | None) -> PendingReview:
    return PendingReview(issue_key=KEY, pr_number=321, pr_url="u", branch_name="231-hand-made", _issue_number=231,
                         agent_label="agent:backend", authority=authority)


def _rework(authority: LaunchAuthority | None) -> PendingRework:
    return PendingRework(issue_key=KEY, agent_type="agent:backend", issue_number=231, pr_number=321,
                         authority=authority)


def _round_trip(kind: PendingWorkKind, request: PendingReview | PendingRework) -> PendingReview | PendingRework:
    restored = decode_claim(encode_claim(PendingWorkClaim(kind=kind, request=request))).request
    assert isinstance(restored, (PendingReview, PendingRework))
    return restored


@pytest.mark.parametrize("authority", [AUTHORITY, None])
def test_a_queued_items_authority_survives_a_restart(authority: LaunchAuthority | None) -> None:
    assert _round_trip(PendingWorkKind.REVIEW, _review(authority)).authority == authority
    assert _round_trip(PendingWorkKind.REWORK, _rework(authority)).authority == authority


def test_a_claim_recorded_before_the_authority_existed_reads_as_none() -> None:
    payload = encode_claim(PendingWorkClaim(kind=PendingWorkKind.REWORK, request=_rework(AUTHORITY)))
    del payload["request"]["authority"]

    restored = decode_claim(payload).request

    assert isinstance(restored, PendingRework) and restored.authority is None


@pytest.mark.parametrize("damage", ["integration", {"effort": "integration", "issue_number": 231, "pr_number": 321}])
def test_a_malformed_authority_is_refused(damage: object) -> None:
    payload = encode_claim(PendingWorkClaim(kind=PendingWorkKind.REVIEW, request=_review(AUTHORITY)))
    payload["request"]["authority"] = damage

    with pytest.raises(Exception, match="authority"):
        decode_claim(payload)


def test_the_planner_queues_work_under_the_authority_discovery_found() -> None:
    config = Config(repo="owner/repo")
    config.code_review_agent, config.code_review_label = "agent:reviewer", "needs-code-review"
    snapshot = OrchestratorSnapshot(
        issues=(), active_sessions=(), pending_reviews=(), pending_reworks=(), pending_tech_lead=(), paused=False,
        discovered_reviews=(DiscoveredReview(231, 321, "u321", "231-hand-made", agent_label="agent:backend",
                                             authority=AUTHORITY),),
        discovered_reworks=(DiscoveredRework(232, 322, "232-hand-made", "agent:frontend", authority=REWORK_AUTHORITY),),
    )

    plan = Planner(config=config, scheduler=Scheduler(config)).plan(snapshot)

    [review] = [a for a in plan.actions if isinstance(a, QueueReviewAction)]
    [rework] = [a for a in plan.actions if isinstance(a, QueueReworkAction)]
    # ... and the queued items the queue handlers build from them.
    queued_review = review.pending(KEY)
    assert (queued_review.pr_number, queued_review.agent_label, queued_review.authority) == (
        321, "agent:backend", AUTHORITY)
    queued_rework = rework.pending(GitHubIssueKey(repo="owner/repo", external_id="232"), "agent:developer")
    assert (queued_rework.pr_number, queued_rework.agent_type, queued_rework.authority) == (
        322, "agent:frontend", REWORK_AUTHORITY)


def test_an_authority_names_its_effort_and_agent() -> None:
    with pytest.raises(ValueError):
        LaunchAuthority(effort="", issue_number=231, pr_number=321, agent="agent:backend")
    with pytest.raises(ValueError):
        LaunchAuthority(effort="integration", issue_number=231, pr_number=321, agent="")
