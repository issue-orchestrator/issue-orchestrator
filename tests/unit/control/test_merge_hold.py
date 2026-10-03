"""The merge hold through the real shared-block owner (#7678)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.completion_pr_labels import apply_pr_labels
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.merge_hold_migration import MergeHoldMigration, MergeHoldMoveStatus
from issue_orchestrator.domain.human_block import BlockOutcome, HumanBlockRequest, NeedsHumanCause
from issue_orchestrator.domain.models import CompletionRecord
from issue_orchestrator.infra.config import Config
from tests.unit.control.test_retained_completion_preparation import _real_block

ISSUE, PR = 364, 379


def _record(*actions: str) -> CompletionRecord:
    return CompletionRecord.from_dict({
        "session_id": "s", "timestamp": "2026-10-02T00:00:00Z", "outcome": "completed",
        "summary": "done", "requested_actions": list(actions),
    })


def test_the_post_publish_clear_never_takes_a_persons_merge_hold_off(tmp_path) -> None:
    """The awaiting-merge "now reworkable" clear withdraws ONLY the engine's
    own escalation: the label stays while a person's merge decision holds it."""
    live: dict[int, set[str]] = {PR: set()}
    block, claims = _real_block(tmp_path, live)
    assert block.acquire(HumanBlockRequest(PR, NeedsHumanCause.MERGE_DECISION, "asked")).committed

    outcome = block.release(HumanBlockRequest(PR, NeedsHumanCause.MERGE_ESCALATION, "reworkable"))

    assert outcome is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert live[PR] == {"needs-human"}
    assert claims.needs_human_causes(PR) == frozenset({NeedsHumanCause.MERGE_DECISION.value})


def test_a_merge_hold_that_does_not_commit_fails_the_publication(tmp_path) -> None:
    block = MagicMock()
    block.acquire.return_value = BlockOutcome.FAILED
    errors: list[str] = []

    applied = apply_pr_labels(
        pr_number=PR, record=_record("create_pr", "hold_merge_for_human"), labels=MagicMock(),
        block=block, actions_taken=[], errors=errors,
    )

    assert applied is False and "merge hold" in errors[0]


def test_stale_cause_rows_are_dropped_where_the_label_is_gone(tmp_path) -> None:
    """porchpin #293/#186/#278: rows left under a label a person took off."""
    live: dict[int, set[str]] = {186: set(), 293: set(), 262: set()}
    block, claims = _real_block(tmp_path, live)
    for number in (186, 293, 262):
        assert block.acquire(HumanBlockRequest(number, NeedsHumanCause.SESSION_LIFECYCLE, "x")).committed
    live[186].clear()
    live[293].clear()

    assert block.forget_stale_causes() == (186, 293)
    assert claims.needs_human_cause_targets() == frozenset({262})


def test_an_unreadable_label_keeps_its_rows(tmp_path) -> None:
    live: dict[int, set[str]] = {186: set()}
    block, claims = _real_block(tmp_path, live)
    assert block.acquire(HumanBlockRequest(186, NeedsHumanCause.SESSION_LIFECYCLE, "x")).committed
    unreadable = replace(block, read_labels=_raise)

    assert unreadable.forget_stale_causes() == ()
    assert claims.needs_human_cause_targets() == frozenset({186})


def _raise(number):
    raise RuntimeError("502")


@dataclass
class _Item:
    number: int
    labels: list[str]
    state: str = "open"


@dataclass
class _Host:
    issues: dict[int, _Item] = field(default_factory=dict)
    prs: dict[int, _Item] = field(default_factory=dict)


def _migration(tmp_path, *, issue_labels=("needs-human", "pr-pending"), pr_issue=ISSUE, causes=None):
    live: dict[int, set[str]] = {ISSUE: set(), PR: set()}
    block, claims = _real_block(tmp_path, live)
    for cause in causes or (NeedsHumanCause.AGENT_COMPLETION,):
        assert block.acquire(HumanBlockRequest(ISSUE, cause, "agent asked")).committed
    live[ISSUE] |= set(issue_labels)
    host = _Host({ISSUE: _Item(ISSUE, sorted(live[ISSUE]))}, {PR: _Item(PR, ["code-reviewed"])})
    migration = MergeHoldMigration(
        block=block, labels=LabelManager(Config()), read_issue=host.issues.get,
        read_pr=host.prs.get, pr_issue_number=lambda pr: pr_issue,
    )
    return migration, live, claims


def test_porchpin_364s_question_moves_to_pr_379s_merge_hold(tmp_path) -> None:
    migration, live, claims = _migration(tmp_path)

    dry = migration.move(ISSUE, PR, apply=False)
    assert dry.status is MergeHoldMoveStatus.WOULD_MOVE and "needs-human" in live[ISSUE]

    moved = migration.move(ISSUE, PR, apply=True)

    assert moved.status is MergeHoldMoveStatus.MOVED, moved.detail
    assert "needs-human" not in live[ISSUE] and claims.needs_human_causes(ISSUE) == frozenset()
    assert "needs-human" in live[PR]
    assert claims.needs_human_causes(PR) == frozenset({NeedsHumanCause.MERGE_DECISION.value})


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"causes": (NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE)}, "not only"),
        ({"causes": (NeedsHumanCause.SESSION_LIFECYCLE,)}, "not only"),
        ({"issue_labels": ("needs-human", "tech-lead-needs-human")}, "handed over"),
        ({"pr_issue": 999}, "is not issue"),
    ],
)
def test_anything_but_an_agents_own_question_on_its_own_pr_is_refused(tmp_path, kwargs, why) -> None:
    migration, live, _claims = _migration(tmp_path, **kwargs)

    refused = migration.move(ISSUE, PR, apply=True)

    assert refused.status is MergeHoldMoveStatus.REFUSED and why in refused.detail
    assert "needs-human" in live[ISSUE] and "needs-human" not in live[PR]
