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
    is_scratch_branch_name,
    is_scratch_worktree_name,
    new_scratch_token,
    path_is_under_scratch_worktree,
    scratch_branch_name,
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
