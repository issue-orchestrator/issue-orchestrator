"""The capability table, pinned row by row (#7347).

Every policy question about a kind of session reads one row of this table.
The matrix below is the specification: a change to any cell is a behaviour
change for every site that asks that capability, and must be deliberate.
"""

import pytest

from issue_orchestrator.domain.session_kind import (
    CompletionProtocol,
    SandboxRole,
    SessionKind,
)

C, RW, R, RR, TL, H = (
    SessionKind.CODE,
    SessionKind.REWORK,
    SessionKind.REVIEW,
    SessionKind.RETROSPECTIVE_REVIEW,
    SessionKind.TECH_LEAD,
    SessionKind.HISTORICAL,
)

# kind: (produces_commits, capturable, open_pr_means_done, holds_issue_custody,
#        killable_generation, completion_protocol, sandbox_role)
_MATRIX = {
    C: (True, True, True, True, True, CompletionProtocol.CODING_DONE, SandboxRole.CODER),
    RW: (True, True, False, False, True, CompletionProtocol.CODING_DONE, SandboxRole.CODER),
    R: (False, False, False, False, False, CompletionProtocol.REVIEWER_DONE, SandboxRole.REVIEWER),
    RR: (False, False, False, False, False, CompletionProtocol.REVIEWER_DONE, SandboxRole.REVIEWER),
    TL: (True, False, False, True, False, CompletionProtocol.CODING_DONE, SandboxRole.TECH_LEAD),
    H: (False, True, False, False, False, CompletionProtocol.NONE, None),
}


def test_the_matrix_covers_every_kind() -> None:
    assert set(_MATRIX) == set(SessionKind)


@pytest.mark.parametrize("kind", list(SessionKind), ids=lambda k: k.value)
def test_each_kind_has_exactly_its_row(kind: SessionKind) -> None:
    capabilities = kind.capabilities
    assert (
        capabilities.produces_commits,
        capabilities.capturable,
        capabilities.open_pr_means_done,
        capabilities.holds_issue_custody,
        capabilities.killable_generation,
        capabilities.completion_protocol,
        capabilities.sandbox_role,
    ) == _MATRIX[kind]


def test_only_a_coding_session_is_done_when_its_branch_has_an_open_pr() -> None:
    """#7343: every other kind starts with the PR it works on already open."""
    assert {k for k in SessionKind if k.capabilities.open_pr_means_done} == {C}


def test_a_tech_lead_run_is_never_captured_as_validated_work() -> None:
    """#7323 / #7346: its completion owns its branch; recovery never takes it."""
    assert not TL.capabilities.capturable
    assert {k for k in SessionKind if k.capabilities.capturable} == {C, RW, H}


def test_only_coding_and_rework_runs_are_killable_generations() -> None:
    assert {k for k in SessionKind if k.capabilities.killable_generation} == {C, RW}


def test_reviewers_report_a_verdict() -> None:
    assert {k for k in SessionKind if k.capabilities.reports_verdict} == {R, RR}
