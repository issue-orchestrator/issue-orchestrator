"""The scratch identity has one owner, and startup reads it (#7263, #6969).

The generator and the matcher already lived together; the identity TYPE, the
name parsers and the pair reading did not, so a caller that needed any of them
re-typed the grammar. Startup reconciliation held the third copy -- and a
classifier that stops matching does not fail, it silently reclassifies: an
external checkout as disposable, or a disposable one as external.
"""

from __future__ import annotations

import re
from pathlib import Path

from issue_orchestrator.control.worktree_reconciliation import _worktree_patterns
from issue_orchestrator.domain.tech_lead_scratch_identity import (
    SCRATCH_TOKEN_LENGTH,
    new_scratch_identity,
)


class TestReconciliationReadsTheOwnersGrammar:
    def test_it_recognises_a_name_the_owner_generated(self) -> None:
        identity = new_scratch_identity("issue-orchestrator", 6410)
        patterns = _worktree_patterns(Path("/dev/issue-orchestrator"))

        assert patterns.scratch.fullmatch(identity.worktree_name)

    def test_a_change_to_the_owners_token_length_travels(self) -> None:
        """The property the third copy could not have.

        A hand-written regex here keeps matching yesterday's names after the
        generator changes; a composed one cannot.
        """
        patterns = _worktree_patterns(Path("/dev/issue-orchestrator"))
        token_length = int(
            re.search(r"\{(\d+)\}", patterns.scratch.pattern).group(1)  # type: ignore[union-attr]
        )

        assert token_length == SCRATCH_TOKEN_LENGTH

    def test_an_ordinary_issue_worktree_is_not_scratch(self) -> None:
        patterns = _worktree_patterns(Path("/dev/issue-orchestrator"))

        assert patterns.ordinary.fullmatch("issue-orchestrator-6410")
        assert not patterns.scratch.fullmatch("issue-orchestrator-6410")

    def test_a_reviewer_worktree_of_either_kind_is_recognised(self) -> None:
        identity = new_scratch_identity("issue-orchestrator", 6410)
        patterns = _worktree_patterns(Path("/dev/issue-orchestrator"))
        stamp = "review-20260917T021721429502Z"

        assert patterns.reviewer.fullmatch(f"issue-orchestrator-6410-{stamp}")
        assert patterns.reviewer.fullmatch(f"{identity.worktree_name}-{stamp}")
