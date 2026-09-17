"""The scratch identity generator and its matcher must not drift apart (#6969).

``control.tech_lead_session_policy`` generates the disposable worktree and branch
for a tech-lead failure investigation; ``domain.timeline_actor`` recognises them
on already-written timeline records. If those two definitions ever diverge, the
discriminator silently stops classifying and the #6969 conflation returns with no
failing test to announce it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.tech_lead_scratch_identity import (
    SCRATCH_TOKEN_LENGTH,
    ScratchWorktreeIdentity,
    is_scratch_branch_name,
    is_scratch_worktree_name,
    new_scratch_identity,
    new_scratch_token,
    path_is_under_scratch_worktree,
    scratch_branch_focus_issue,
    scratch_branch_name,
    names_one_scratch_checkout,
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


class TestPairMatching:
    """Do a worktree basename and a branch describe the same run? (#7263)"""

    def _launched(self, issue: int = 6410) -> ScratchWorktreeIdentity:
        return new_scratch_identity("issue-orchestrator", issue)

    def test_a_launched_investigations_own_pair_agrees(self) -> None:
        launched = self._launched()

        assert names_one_scratch_checkout(
            launched.worktree_name, launched.branch_name
        )

    def test_an_ordinary_issue_worktree_is_not_a_pair(self) -> None:
        assert not names_one_scratch_checkout(
            "issue-orchestrator-6410", "6410-fix-the-thing"
        )

    @pytest.mark.parametrize(
        "worktree,branch",
        [
            ("issue-orchestrator-6410", "SCRATCH_BRANCH"),
            ("SCRATCH_WORKTREE", "6410-fix-the-thing"),
        ],
        ids=["scratch branch, ordinary worktree", "scratch worktree, ordinary branch"],
    )
    def test_a_half_scratch_pair_does_not_agree(
        self, worktree: str, branch: str
    ) -> None:
        launched = self._launched()
        worktree = worktree.replace("SCRATCH_WORKTREE", launched.worktree_name)
        branch = branch.replace("SCRATCH_BRANCH", launched.branch_name)

        assert not names_one_scratch_checkout(worktree, branch)

    def test_halves_from_two_runs_of_one_issue_do_not_agree(self) -> None:
        """The case matching issue numbers alone would have accepted.

        Both halves are well-formed and both name issue 6410, but they are from
        DIFFERENT investigations, so together they describe no single checkout.
        Only the run token separates them, so the tokens are spelled out rather
        than minted: asserting that two random ones differ is a probability
        statement, not a property.
        """
        first = scratch_worktree_name("issue-orchestrator", 6410, "a" * 12)
        second = scratch_branch_name(6410, "b" * 12)

        assert not names_one_scratch_checkout(first, second)

    def test_two_issues_investigations_do_not_agree(self) -> None:
        mine = self._launched(6410)
        theirs = self._launched(6411)

        assert not names_one_scratch_checkout(mine.worktree_name, theirs.branch_name)

    def test_an_empty_half_does_not_agree(self) -> None:
        launched = self._launched()

        assert not names_one_scratch_checkout("", launched.branch_name)
        assert not names_one_scratch_checkout(launched.worktree_name, "")
