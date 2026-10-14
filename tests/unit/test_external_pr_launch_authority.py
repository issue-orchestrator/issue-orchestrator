"""Queued work on an external PR keeps the authority it was queued under (#9499 r4 F5).

Discovery records the integration effort that admitted a review or a rework;
the planner's queue actions and the pending items carry it, and it survives a
restart, so the launch re-judges the work under that authority however the
PR's labels or base change meanwhile.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.actions import QueueReviewAction, QueueReworkAction
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import DiscoveredReview, DiscoveredRework, PendingReview, PendingRework
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.execution.pending_work_codec import decode_claim, encode_claim
from issue_orchestrator.infra.config import Config

KEY = GitHubIssueKey(repo="owner/repo", external_id="231")


def _review(effort: str | None) -> PendingReview:
    return PendingReview(issue_key=KEY, pr_number=321, pr_url="u", branch_name="231-hand-made", _issue_number=231,
                         governing_effort=effort)


def _rework(effort: str | None) -> PendingRework:
    return PendingRework(issue_key=KEY, agent_type="agent:backend", issue_number=231, pr_number=321,
                         governing_effort=effort)


def _round_trip(kind: PendingWorkKind, request: PendingReview | PendingRework) -> PendingReview | PendingRework:
    restored = decode_claim(encode_claim(PendingWorkClaim(kind=kind, request=request))).request
    assert isinstance(restored, (PendingReview, PendingRework))
    return restored


@pytest.mark.parametrize("effort", ["integration", None])
def test_a_queued_items_governing_effort_survives_a_restart(effort: str | None) -> None:
    assert _round_trip(PendingWorkKind.REVIEW, _review(effort)).governing_effort == effort
    assert _round_trip(PendingWorkKind.REWORK, _rework(effort)).governing_effort == effort


def test_a_claim_recorded_before_the_authority_existed_reads_as_none() -> None:
    payload = encode_claim(PendingWorkClaim(kind=PendingWorkKind.REWORK, request=_rework("integration")))
    del payload["request"]["governing_effort"]

    restored = decode_claim(payload).request

    assert isinstance(restored, PendingRework) and restored.governing_effort is None


def test_a_malformed_authority_is_refused() -> None:
    payload = encode_claim(PendingWorkClaim(kind=PendingWorkKind.REVIEW, request=_review("integration")))
    payload["request"]["governing_effort"] = 7

    with pytest.raises(Exception, match="governing_effort"):
        decode_claim(payload)


def test_the_planner_queues_work_under_the_authority_discovery_found() -> None:
    config = Config(repo="owner/repo")
    config.code_review_agent, config.code_review_label = "agent:reviewer", "needs-code-review"
    snapshot = OrchestratorSnapshot(
        issues=(), active_sessions=(), pending_reviews=(), pending_reworks=(), pending_tech_lead=(), paused=False,
        discovered_reviews=(DiscoveredReview(231, 321, "u321", "231-hand-made", governing_effort="integration"),),
        discovered_reworks=(DiscoveredRework(232, 322, "232-hand-made", "agent:backend",
                                             governing_effort="integration"),),
    )

    plan = Planner(config=config, scheduler=Scheduler(config)).plan(snapshot)

    [review] = [a for a in plan.actions if isinstance(a, QueueReviewAction)]
    [rework] = [a for a in plan.actions if isinstance(a, QueueReworkAction)]
    assert (review.pr_number, review.governing_effort) == (321, "integration")
    assert (rework.pr_number, rework.governing_effort) == (322, "integration")
