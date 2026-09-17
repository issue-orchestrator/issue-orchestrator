"""The scratch identity generator and its matcher must not drift apart (#6969).

``control.tech_lead_session_policy`` generates the disposable worktree and branch
for a tech-lead failure investigation; ``domain.timeline_actor`` recognises them
on already-written timeline records. If those two definitions ever diverge, the
discriminator silently stops classifying and the #6969 conflation returns with no
failing test to announce it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from issue_orchestrator.domain.tech_lead_scratch_identity import (
    SCRATCH_TOKEN_LENGTH,
    ScratchIdentityVerdict,
    ScratchWorktreeIdentity,
    is_scratch_branch_name,
    is_scratch_worktree_name,
    new_scratch_identity,
    new_scratch_token,
    path_is_under_scratch_worktree,
    read_scratch_identity,
    scratch_branch_focus_issue,
    scratch_branch_name,
    scratch_worktree_focus_issue,
    scratch_worktree_name,
)


class TestGeneratorMatcherAgreement:
    @pytest.mark.parametrize("repo", ["issue-orchestrator", "porchpin", "a-b-c"])
    @pytest.mark.parametrize("issue_number", [1, 6410, 123456])
    def test_every_generated_name_is_recognised(
        self, repo: str, issue_number: int
    ) -> None:
        token = new_scratch_token()

        assert is_scratch_worktree_name(
            scratch_worktree_name(repo, issue_number, token)
        )
        assert is_scratch_branch_name(scratch_branch_name(issue_number, token))

    def test_token_length_is_stable(self) -> None:
        # The matcher pins the token length; a generator change that widened it
        # would stop matching every previously written record.
        assert len(new_scratch_token()) == SCRATCH_TOKEN_LENGTH

    def test_investigation_branch_does_not_start_with_the_focus_number(self) -> None:
        # extract_issue_number_from_branch must never mistake the disposable
        # branch for the focus issue's own branch (#6823).
        branch = scratch_branch_name(6410, new_scratch_token())

        assert not branch.startswith("6410")


class TestNonScratchNamesAreRejected:
    @pytest.mark.parametrize(
        "name",
        [
            "issue-orchestrator-6410",
            "issue-orchestrator",
            "issue-orchestrator-tech-lead-6410",
            "issue-orchestrator-tech-lead-6410-short",
            "issue-orchestrator-tech-lead-abc-df24fde45b3b",
            "issue-orchestrator-tech-lead-6410-DF24FDE45B3B",
            "",
        ],
    )
    def test_worktree_matcher_rejects(self, name: str) -> None:
        assert not is_scratch_worktree_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "tech-lead-investigation-6410",
            "6410-tech-lead-investigation-df24fde45b3b",
            "tech-lead-6410-df24fde45b3b",
            "",
        ],
    )
    def test_branch_matcher_rejects(self, name: str) -> None:
        assert not is_scratch_branch_name(name)


class TestPathMatching:
    def test_nested_run_dir_matches_through_its_ancestor(self) -> None:
        token = new_scratch_token()
        worktree = scratch_worktree_name("issue-orchestrator", 6410, token)
        run_dir = Path("/Users/dev") / worktree / ".issue-orchestrator/sessions/run-1"

        assert path_is_under_scratch_worktree(str(run_dir))

    def test_ordinary_issue_worktree_does_not_match(self) -> None:
        assert not path_is_under_scratch_worktree(
            "/Users/dev/issue-orchestrator-6410/.issue-orchestrator/sessions/run-1"
        )

    def test_empty_path_does_not_match(self) -> None:
        assert not path_is_under_scratch_worktree("")


class TestFreshIdentity:
    def test_both_halves_share_one_token(self) -> None:
        """The worktree and the branch of one run must agree, or a later resume
        reads them as a corrupt pair."""
        identity = new_scratch_identity("issue-orchestrator", 6410)

        assert scratch_worktree_focus_issue(identity.worktree_name) == 6410
        assert scratch_branch_focus_issue(identity.branch_name) == 6410
        assert identity.worktree_name.endswith(identity.branch_name.rsplit("-", 1)[-1])

    def test_distinct_tokens_give_distinct_identities(self) -> None:
        """Deterministic on purpose.

        Asserting that two freshly minted identities differ is a probability
        statement, not a property: two 48-bit tokens CAN collide, so the test
        would be asserting odds. What the code actually promises is that a
        different token yields a different worktree AND a different branch, and
        that is what is checked -- the token supply is tested separately below.
        """
        first = ScratchWorktreeIdentity(
            worktree_name=scratch_worktree_name("issue-orchestrator", 6410, "a" * 12),
            branch_name=scratch_branch_name(6410, "a" * 12),
        )
        second = ScratchWorktreeIdentity(
            worktree_name=scratch_worktree_name("issue-orchestrator", 6410, "b" * 12),
            branch_name=scratch_branch_name(6410, "b" * 12),
        )

        assert first.worktree_name != second.worktree_name
        assert first.branch_name != second.branch_name

    def test_the_token_supply_is_the_declared_shape(self) -> None:
        """What makes a collision unlikely, stated as the property it is."""
        token = new_scratch_token()

        assert len(token) == SCRATCH_TOKEN_LENGTH
        assert all(character in "0123456789abcdef" for character in token)


class TestReadingARecordedPair:
    """Resuming an investigation that is already on disk (#7263)."""

    def _launched(self, issue: int = 6410) -> ScratchWorktreeIdentity:
        return new_scratch_identity("issue-orchestrator", issue)

    def test_a_launched_investigation_is_resumed_exactly(self) -> None:
        """Read back, never re-minted: a new token would strand the commits the
        retry exists to re-validate on the old branch."""
        launched = self._launched()

        reading = read_scratch_identity(
            f"/Users/dev/worktree/{launched.worktree_name}",
            launched.branch_name,
            6410,
        )

        assert reading.verdict is ScratchIdentityVerdict.RESUMABLE
        assert reading.identity == launched

    def test_an_ordinary_retry_is_not_an_investigation(self) -> None:
        reading = read_scratch_identity(
            "/Users/dev/issue-orchestrator-6410", "6410-fix-the-thing", 6410
        )

        assert reading.verdict is ScratchIdentityVerdict.ORDINARY
        assert reading.identity is None
        assert reading.is_corrupt is False

    def test_a_run_directory_inside_the_scratch_worktree_is_not_one(self) -> None:
        """The path's OWN basename must be the worktree; a run dir nested inside
        it is not a checkout to reuse, and reusing it would put the worktree
        somewhere it has never been."""
        launched = self._launched()
        run_dir = (
            f"/Users/dev/{launched.worktree_name}/.issue-orchestrator/sessions/run-1"
        )

        assert read_scratch_identity(run_dir, launched.branch_name, 6410).is_corrupt

    @pytest.mark.parametrize(
        "worktree_path,branch",
        [
            ("/Users/dev/issue-orchestrator-6410", "SCRATCH_BRANCH"),
            ("/Users/dev/SCRATCH_WORKTREE", "6410-fix-the-thing"),
        ],
        ids=["scratch branch, ordinary worktree", "scratch worktree, ordinary branch"],
    )
    def test_a_half_scratch_pair_is_corrupt(
        self, worktree_path: str, branch: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Guessing the missing half would resume the wrong checkout: an
        investigation branch inside the focus issue's own worktree is the exact
        mutation #6823 forbids. CORRUPT, not ORDINARY -- the caller must refuse,
        not fall back."""
        launched = self._launched()
        worktree_path = worktree_path.replace("SCRATCH_WORKTREE", launched.worktree_name)
        branch = branch.replace("SCRATCH_BRANCH", launched.branch_name)

        with caplog.at_level(logging.WARNING):
            reading = read_scratch_identity(worktree_path, branch, 6410)

        assert reading.verdict is ScratchIdentityVerdict.CORRUPT
        assert reading.identity is None
        assert "only one half names an investigation" in caplog.text

    def test_halves_from_two_runs_of_one_issue_are_corrupt(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The case matching issue numbers alone would have accepted.

        Both halves are well-formed and both name issue 6410, but they are from
        DIFFERENT investigations -- resuming that pair checks one run's branch
        out inside another run's directory. Only the run token separates them.
        """
        first = self._launched()
        second = self._launched()

        with caplog.at_level(logging.WARNING):
            reading = read_scratch_identity(
                f"/Users/dev/{first.worktree_name}", second.branch_name, 6410
            )

        assert reading.verdict is ScratchIdentityVerdict.CORRUPT
        assert "different investigation runs" in caplog.text

    def test_another_issues_investigation_is_corrupt(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A retry queued for 6410 must never resume 6411's investigation.

        Both halves parse and agree with each other; only the retry's own issue
        disagrees, which is the case a self-consistency check alone would miss.
        """
        launched = self._launched(6411)

        with caplog.at_level(logging.WARNING):
            reading = read_scratch_identity(
                f"/Users/dev/{launched.worktree_name}", launched.branch_name, 6410
            )

        assert reading.verdict is ScratchIdentityVerdict.CORRUPT
        assert "belongs to issue 6411" in caplog.text

    def test_an_empty_worktree_path_with_a_scratch_branch_is_corrupt(self) -> None:
        launched = self._launched()

        assert read_scratch_identity("", launched.branch_name, 6410).is_corrupt

    def test_nothing_recorded_at_all_is_ordinary(self) -> None:
        reading = read_scratch_identity("", "", 6410)

        assert reading.verdict is ScratchIdentityVerdict.ORDINARY
