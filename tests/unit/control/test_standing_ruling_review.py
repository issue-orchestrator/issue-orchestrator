"""The review rule of standing rulings (#8141).

porchpin#379: the reviewer approved, twice, a diff extending exactly what the
maintainer's ruling retired. An approval of a diff the ruling governs must now
attest the ruling, or the orchestrator refuses it and sends the PR back with
the ruling as implementation-required feedback, on BOTH review paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from issue_orchestrator.control.completion_ports import GitAdapter, LabelAdapter, PRAdapter
from issue_orchestrator.control.standing_ruling_review import StandingRulingsReview
from issue_orchestrator.domain.models import CompletionOutcome, CompletionRecord, RequestedAction
from issue_orchestrator.domain.standing_ruling import REFUSED_APPROVAL_MARKER
from issue_orchestrator.domain.events import EventBus
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import BranchPathsResult
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.run_allocation_helpers import make_completion_processor
from tests.standing_ruling_helpers import IssueBodies, a_ruling, body_with, rulings_owner
from tests.unit.session_run_helpers import make_session_run_assets

ISSUE, PR = 364, 379
WALK = a_ruling("m-00000000walk", files=("tools/walk",))
WHOLE = a_ruling("m-0000000whole")


def _approval(*, upheld: list[str] | None = None) -> CompletionRecord:
    return CompletionRecord(
        session_id="review-379", timestamp="2026-10-04T14:36:00", outcome=CompletionOutcome.REVIEW_APPROVED,
        summary="Approved", requested_actions=[
            RequestedAction.ADD_CODE_REVIEWED_LABEL, RequestedAction.REMOVE_NEEDS_REWORK_LABEL,
            RequestedAction.REMOVE_CODE_REVIEW_LABEL, RequestedAction.POST_COMMENT,
        ],
        review_summary="The new symbol-resolved provenance check closes the planned clause.",
        risk_level="low", checks_passed=["tests"], comment_body="## Code Review Approved", upheld_rulings=upheld,
    )


def _review(*rulings, paths=("tools/walk/check.py",)) -> tuple[StandingRulingsReview, Mock]:
    owner = rulings_owner(IssueBodies({ISSUE: body_with(*rulings)}))
    changed = Mock(return_value=tuple(paths))
    return StandingRulingsReview(owner, changed), changed


class TestThePostPublishReviewDoor:
    def test_an_unattested_approval_of_a_governed_diff_becomes_changes_requested(self) -> None:
        review, _ = _review(WALK)

        admitted = review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR)

        assert admitted.outcome is CompletionOutcome.REVIEW_CHANGES_REQUESTED
        assert admitted.requested_actions == [
            RequestedAction.ADD_NEEDS_REWORK_LABEL, RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            RequestedAction.POST_COMMENT,
        ]
        assert admitted.review_issues is not None and admitted.review_issues.startswith("Implementation-required")
        assert "`m-00000000walk`" in admitted.review_issues
        assert admitted.comment_body is not None and REFUSED_APPROVAL_MARKER in admitted.comment_body
        assert "request_changes" in admitted.comment_body and admitted.upheld_rulings is None

    def test_an_attested_approval_stands(self) -> None:
        review, _ = _review(WALK)
        approval = _approval(upheld=[WALK.ruling_id])

        assert review.admit(approval, issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR) is approval

    def test_a_diff_outside_every_ruling_is_approvable_without_attestation(self) -> None:
        review, _ = _review(WALK, paths=("docs/readme.md",))

        assert review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR).outcome is (
            CompletionOutcome.REVIEW_APPROVED
        )

    def test_a_whole_issue_ruling_governs_every_diff(self) -> None:
        review, _ = _review(WHOLE, paths=("docs/readme.md",))

        assert review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR).outcome is (
            CompletionOutcome.REVIEW_CHANGES_REQUESTED
        )

    def test_attesting_one_ruling_does_not_cover_another(self) -> None:
        review, _ = _review(WALK, WHOLE)

        refused = review.admit(_approval(upheld=[WHOLE.ruling_id]), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR)

        assert refused.review_issues is not None and "m-00000000walk" in refused.review_issues
        assert "m-0000000whole" not in refused.review_issues

    def test_an_issue_with_no_rulings_never_reads_the_diff(self) -> None:
        review, changed = _review()

        assert review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR).outcome is (
            CompletionOutcome.REVIEW_APPROVED
        )
        changed.assert_not_called()

    def test_changes_requested_passes_through(self) -> None:
        review, changed = _review(WALK)
        record = CompletionRecord(session_id="s", timestamp="t", outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
                                  summary="Fix", review_issues="Fix it")

        assert review.admit(record, issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR) is record
        changed.assert_not_called()

    def test_an_unreadable_diff_fails_the_review_loudly(self) -> None:
        owner = rulings_owner(IssueBodies({ISSUE: body_with(WALK)}))
        review = StandingRulingsReview(owner, Mock(side_effect=RuntimeError("git said no")))

        with pytest.raises(RuntimeError, match="git said no"):
            review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR)


def test_a_retrospective_review_with_no_branch_diff_must_attest_every_ruling() -> None:
    """Merged work reviewed as it stands has no diff to scope by: every ruling governs it."""
    review, _ = _review(WALK, paths=())

    refused = review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=None)

    assert refused.outcome is CompletionOutcome.REVIEW_CHANGES_REQUESTED
    assert review.admit(_approval(upheld=[WALK.ruling_id]), issue_number=ISSUE, worktree=Path("/wt"),
                        pr_number=None).outcome is CompletionOutcome.REVIEW_APPROVED


def test_deleting_or_renaming_away_a_ruled_file_needs_the_attestation_too() -> None:
    """The door reads every touched path (deletions and rename sources included)."""
    review, _ = _review(WALK, paths=("tools/walk/check.py", "tools/moved.py"))  # check.py renamed away

    assert review.admit(_approval(), issue_number=ISSUE, worktree=Path("/wt"), pr_number=PR).outcome is (
        CompletionOutcome.REVIEW_CHANGES_REQUESTED
    )


class TestTheReviewExchangeGate:
    def test_an_unattested_ok_becomes_another_coder_round(self) -> None:
        review, _ = _review(WALK)

        reason = review.exchange_gate(ISSUE, Path("/wt"), None).rejection_reason(upheld_rulings=())

        assert reason is not None and "`m-00000000walk`" in reason

    def test_an_attested_ok_is_accepted(self) -> None:
        review, _ = _review(WALK)

        assert review.exchange_gate(ISSUE, Path("/wt"), None).rejection_reason(upheld_rulings=(WALK.ruling_id,)) is None

    def test_a_cached_approval_does_not_outlive_a_ruling_recorded_after_it(self) -> None:
        """Judged again on the rulings standing NOW with its own decision's attestations."""
        bodies = IssueBodies({ISSUE: body_with(WALK)})
        owner = rulings_owner(bodies)
        gate = StandingRulingsReview(owner, Mock(return_value=("tools/walk/check.py",))).exchange_gate(
            ISSUE, Path("/wt"), None)
        assert gate.cached_approval_reason(upheld_rulings=(WALK.ruling_id,)) is None

        bodies.bodies[ISSUE] = body_with(WALK, WHOLE)  # a maintainer rules after the approval

        reason = gate.cached_approval_reason(upheld_rulings=(WALK.ruling_id,))
        assert reason is not None and "m-0000000whole" in reason

    def test_the_tech_lead_gate_runs_first(self) -> None:
        review, changed = _review(WALK)
        other = Mock()
        other.rejection_reason.return_value = "Tech Lead decision artifact rejected"

        reason = review.exchange_gate(ISSUE, Path("/wt"), other).rejection_reason(upheld_rulings=())

        assert reason == "Tech Lead decision artifact rejected"
        other.rejection_reason.assert_called_once_with(upheld_rulings=())
        changed.assert_not_called()


def _pr(base: str | None = "main") -> PRInfo:
    return PRInfo(number=PR, title="t", url="u", branch="364-walk", body="", state="open", labels=[],
                  base_branch=base)


def _processor(tmp_path: Path, *, approval: CompletionRecord, labels: Mock, pr=None) -> tuple:
    git = Mock(spec=GitAdapter)
    git.get_current_branch.return_value = "364-walk"
    git.branch_touched_paths_against_base.return_value = BranchPathsResult(
        success=True, paths=("tools/walk/check.py",)
    )
    pr_adapter = Mock(spec=PRAdapter)
    pr_adapter.get_pr.return_value = pr if pr is not None else _pr()
    owner = rulings_owner(IssueBodies({ISSUE: body_with(WALK)}))
    processor = make_completion_processor(
        agent_callback_endpoint=ready_callback_endpoint(), label_adapter=labels, pr_adapter=pr_adapter,
        git_adapter=git, event_bus=EventBus(), session_output=FileSystemSessionOutput(),
        label_config={"code_reviewed": "code-reviewed", "needs_rework": "needs-rework",
                      "code_review": "needs-code-review"},
        standing_rulings=owner,
    )
    worktree = tmp_path / "worktree"
    (worktree / ".issue-orchestrator").mkdir(parents=True)
    (worktree / ".issue-orchestrator" / "completion.json").write_text(json.dumps(approval.to_dict()))
    result = processor.process(
        worktree, run_assets=make_session_run_assets(worktree), issue_number=ISSUE, issue_title="#364",
        pr_number=PR,
    )
    return result, git, owner


def test_the_completion_processor_refuses_porchpin_379s_approval(tmp_path: Path) -> None:
    """End to end through the completion door: the approval's own labels are
    never written; the PR goes back for rework with the ruling."""
    labels = Mock(spec=LabelAdapter)

    result, git, owner = _processor(tmp_path, approval=_approval(), labels=labels)

    assert result.success
    labels.add_label.assert_called_once_with(PR, "needs-rework")
    assert call(PR, "code-reviewed") not in labels.add_label.call_args_list
    git.branch_touched_paths_against_base.assert_called_once()


def test_a_stacked_or_retargeted_pr_is_judged_against_its_current_base(tmp_path: Path) -> None:
    """The base is read fresh from the PR the review reviewed, never a cached copy."""
    _, git, _ = _processor(tmp_path, approval=_approval(upheld=[WALK.ruling_id]), labels=Mock(spec=LabelAdapter),
                           pr=_pr("363-predecessor"))

    assert git.branch_touched_paths_against_base.call_args.args[1] == "origin/363-predecessor"


def test_a_pr_whose_base_cannot_be_read_fails_the_review_loudly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="base branch"):
        _processor(tmp_path, approval=_approval(), labels=Mock(spec=LabelAdapter), pr=_pr(None))
