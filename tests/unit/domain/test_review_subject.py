"""The branch-naming policy, tested where it now lives (#7263, #7268).

These cases used to be spread across the two emitters that each held their own
copy of the policy. They belong to :class:`BranchSubject`, and the detached-HEAD
case can ONLY be tested here: on the real path a checkout whose branch cannot be
read fails the publish gate long before any review event is emitted.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.domain.review_subject import BranchSubject


class _Checkout:
    def __init__(self, branch: str | None) -> None:
        self.branch = branch
        self.reads: list[Path] = []

    def get_current_branch(self, worktree: Path) -> str | None:
        self.reads.append(worktree)
        return self.branch


class _Retained:
    def __init__(self, branch_name: str | None) -> None:
        self.branch_name = branch_name


class TestSampling:
    def test_a_readable_checkout_names_its_branch(self, tmp_path: Path) -> None:
        checkout = _Checkout("6410-fix")

        assert BranchSubject.sampled(checkout, tmp_path).branch_name == "6410-fix"
        assert checkout.reads == [tmp_path]

    def test_a_detached_checkout_names_nothing(self, tmp_path: Path) -> None:
        """``get_current_branch`` contracts to return None, not to raise."""
        assert BranchSubject.sampled(_Checkout(None), tmp_path).branch_name is None

    def test_no_worktree_is_not_read_at_all(self) -> None:
        checkout = _Checkout("6410-fix")

        assert BranchSubject.sampled(checkout, None).branch_name is None
        assert checkout.reads == []


class TestRetention:
    def test_a_retained_branch_is_replayed(self) -> None:
        assert BranchSubject.retained(_Retained("6410-fix")).branch_name == "6410-fix"

    def test_an_artifact_without_one_names_nothing(self) -> None:
        assert BranchSubject.retained(_Retained(None)).branch_name is None
        assert BranchSubject.retained(None).branch_name is None

    def test_retained_beats_the_current_checkout(self, tmp_path: Path) -> None:
        """The #7268 rule: a replay names the branch its work happened on.

        PR-collision remediation renames the branch after the review, so the
        checkout answers for now, not for the review being replayed.
        """
        subject = BranchSubject.retained(_Retained("the-branch-reviewed")).or_else(
            BranchSubject.sampled(_Checkout("renamed-since"), tmp_path)
        )

        assert subject.branch_name == "the-branch-reviewed"

    def test_sampling_fills_in_when_nothing_was_retained(self, tmp_path: Path) -> None:
        """Summaries written before retention existed still name a branch.

        Dropping the field for them was measured to be worse than the rare
        post-rename inaccuracy: every cache hit WITHOUT a rename is the common
        case, and losing the branch there changes attribution (#7269 F2).
        """
        subject = BranchSubject.retained(_Retained(None)).or_else(
            BranchSubject.sampled(_Checkout("still-the-same"), tmp_path)
        )

        assert subject.branch_name == "still-the-same"


class TestRendering:
    def test_a_named_branch_becomes_one_field(self) -> None:
        assert BranchSubject.named("6410-fix").as_event_fields() == {
            "branch_name": "6410-fix"
        }

    def test_an_unknown_branch_adds_nothing(self) -> None:
        """Absent, not null: consumers distinguish "no branch" from "branch=None"."""
        assert BranchSubject.unknown().as_event_fields() == {}

    def test_blank_is_the_same_as_absent(self) -> None:
        assert BranchSubject.named("   ").as_event_fields() == {}
        assert BranchSubject.named(" 6410-fix ").branch_name == "6410-fix"
