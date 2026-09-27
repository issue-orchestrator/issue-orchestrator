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


# ---------------------------------------------------------------------------
# The consumers ask the table: one test per migrated question
# ---------------------------------------------------------------------------


def test_the_completion_protocol_picks_the_completion_instructions() -> None:
    from issue_orchestrator.resources import (
        get_coding_done_instructions,
        get_completion_instructions,
        get_reviewer_done_instructions,
    )

    assert get_completion_instructions(TL.value) == get_coding_done_instructions()
    assert get_completion_instructions(RW.value) == get_coding_done_instructions()
    assert get_completion_instructions(RR.value) == get_reviewer_done_instructions()
    with pytest.raises(ValueError, match="no agent completion protocol"):
        get_completion_instructions(H.value)


@pytest.mark.parametrize(
    ("task_kind", "role"),
    [
        (C.value, SandboxRole.CODER),
        (RW.value, SandboxRole.CODER),
        (R.value, SandboxRole.REVIEWER),
        (TL.value, SandboxRole.TECH_LEAD),
        (H.value, SandboxRole.CODER),  # no agent: the most bounded floor
        ("review_exchange_reviewer", SandboxRole.REVIEWER),
        ("mystery", SandboxRole.CODER),
    ],
)
def test_the_sandbox_role_comes_from_the_row(task_kind: str, role: SandboxRole) -> None:
    from issue_orchestrator.domain.sandbox_scope import _role_for_task_kind

    assert _role_for_task_kind(task_kind) is role


def test_the_review_prompt_default_follows_the_protocol(tmp_path) -> None:
    from issue_orchestrator.domain.models import DEFAULT_REVIEW_INITIAL_PROMPT, AgentConfig

    config = AgentConfig(prompt_path=tmp_path / "p.md")
    assert config._initial_prompt_template(RR.value) == DEFAULT_REVIEW_INITIAL_PROMPT
    assert config._initial_prompt_template(TL.value) != DEFAULT_REVIEW_INITIAL_PROMPT


def test_only_a_killable_generation_may_be_killed_by_the_tech_lead() -> None:
    from issue_orchestrator.control.tech_lead_kill_session import kill_hung_session_stale_reason

    def reason(kind: SessionKind) -> str | None:
        return kill_hung_session_stale_reason(
            issue_number=7, target_session_id="run-1", target_terminal_id="t-7",
            target_session_type=kind.value,
        )

    assert reason(C) is None and reason(RW) is None
    assert "non-killable" in (reason(TL) or "")
    assert "non-killable" in (reason(R) or "")


def test_the_issue_runtime_lanes_are_the_killable_kinds_lanes() -> None:
    from issue_orchestrator.control.review_exchange_lifecycle import ISSUE_RUNTIME_SESSION_TYPES
    from issue_orchestrator.domain.session_kind import SessionType

    assert ISSUE_RUNTIME_SESSION_TYPES == (SessionType.ISSUE, SessionType.REWORK)


@pytest.mark.parametrize(
    ("kind", "optional"), [(C, False), (RW, False), (H, False), (TL, True), (R, True), (None, False)]
)
def test_only_a_non_deliverable_completion_may_publish_nothing(kind, optional) -> None:
    from issue_orchestrator.domain.registered_completion import CompletionProcessingPolicy

    assert CompletionProcessingPolicy("agent:x", kind).publication_is_optional is optional


@pytest.mark.parametrize(
    ("agent", "work_item"),
    [("agent:tech-lead", False), ("agent:backend", True), (None, True)],
)
def test_a_tech_lead_anchor_is_not_a_work_item(agent, work_item) -> None:
    assert SessionKind.issue_is_work_item(agent, "agent:tech-lead") is work_item


def test_a_retry_decodes_a_pre_7347_tech_lead_stamp() -> None:
    assert SessionKind.from_retry_stamp("code", carries_authority=True) is TL
    assert SessionKind.from_retry_stamp("code", carries_authority=False) is C
    assert SessionKind.from_retry_stamp("rework", carries_authority=False) is RW
