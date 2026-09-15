"""Session identity carried on every session-keyed timeline event (#6969)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.domain.session_event_identity import (
    SessionEventIdentity,
    timeline_actor_for_session,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_scratch_identity import (
    new_scratch_token,
    scratch_branch_name,
    scratch_worktree_name,
)
from issue_orchestrator.domain.timeline_actor import (
    TIMELINE_ACTOR_FIELD,
    TimelineActor,
)

from tests.unit.session_run_helpers import make_session_run_assets


@pytest.fixture
def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.md"
    path.write_text("prompt", encoding="utf-8")
    return path


def _session(
    tmp_path: Path,
    prompt_path: Path,
    *,
    worktree_name: str,
    branch_name: str,
    scratch_worktree: bool,
    issue_number: int = 6410,
) -> Session:
    worktree = tmp_path / worktree_name
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue_number)), task=TaskKind.CODE),
        issue=Issue(number=issue_number, title="Subject", labels=["agent:test"]),
        agent_config=AgentConfig(prompt_path=prompt_path, model="sonnet"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=worktree,
        branch_name=branch_name,
        run_assets=make_session_run_assets(worktree),
        agent_label="agent:tech-lead",
        started_at=datetime(2026, 7, 28, 2, 27, 0),
        scratch_worktree=scratch_worktree,
    )


class TestTimelineActorForSession:
    def test_live_investigation_is_declared_by_its_scratch_flag(
        self, tmp_path: Path, prompt_path: Path
    ) -> None:
        token = new_scratch_token()
        session = _session(
            tmp_path,
            prompt_path,
            worktree_name=scratch_worktree_name("issue-orchestrator", 6410, token),
            branch_name=scratch_branch_name(6410, token),
            scratch_worktree=True,
        )

        assert (
            timeline_actor_for_session(session)
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )

    def test_restored_investigation_is_recovered_from_its_worktree(
        self, tmp_path: Path, prompt_path: Path
    ) -> None:
        # A session rebuilt after an orchestrator restart has no producer to set
        # scratch_worktree, so the durable worktree name has to answer for it.
        token = new_scratch_token()
        session = _session(
            tmp_path,
            prompt_path,
            worktree_name=scratch_worktree_name("issue-orchestrator", 6410, token),
            branch_name=scratch_branch_name(6410, token),
            scratch_worktree=False,
        )

        assert (
            timeline_actor_for_session(session)
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )

    def test_restored_investigation_is_recovered_from_its_branch(
        self, tmp_path: Path, prompt_path: Path
    ) -> None:
        token = new_scratch_token()
        session = _session(
            tmp_path,
            prompt_path,
            worktree_name="relocated-worktree",
            branch_name=scratch_branch_name(6410, token),
            scratch_worktree=False,
        )

        assert (
            timeline_actor_for_session(session)
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )

    def test_ordinary_coding_session_is_the_issues_own_work(
        self, tmp_path: Path, prompt_path: Path
    ) -> None:
        session = _session(
            tmp_path,
            prompt_path,
            worktree_name="issue-orchestrator-6410",
            branch_name="6410-fix-the-thing",
            scratch_worktree=False,
        )

        assert timeline_actor_for_session(session) is TimelineActor.ISSUE_SESSION


class TestSessionEventIdentity:
    def test_event_fields_carry_the_actor_alongside_the_subject(
        self, tmp_path: Path, prompt_path: Path
    ) -> None:
        token = new_scratch_token()
        session = _session(
            tmp_path,
            prompt_path,
            worktree_name=scratch_worktree_name("issue-orchestrator", 6410, token),
            branch_name=scratch_branch_name(6410, token),
            scratch_worktree=True,
        )

        fields = SessionEventIdentity.of(session).as_event_fields()

        assert fields == {
            "issue_number": 6410,
            "session_id": "issue-6410",
            "agent": "agent:tech-lead",
            "task": "code",
            "rework_cycle": None,
            TIMELINE_ACTOR_FIELD: "tech-lead-investigation",
        }

    def test_subject_issue_alone_never_identifies_the_producer(
        self, tmp_path: Path, prompt_path: Path
    ) -> None:
        # Both sessions are keyed to issue 6410 and both call themselves
        # issue-6410 with task "code" -- the actor is the only field that
        # separates them, which is exactly why #6969 happened.
        token = new_scratch_token()
        investigation = SessionEventIdentity.of(
            _session(
                tmp_path / "a",
                prompt_path,
                worktree_name=scratch_worktree_name("issue-orchestrator", 6410, token),
                branch_name=scratch_branch_name(6410, token),
                scratch_worktree=True,
            )
        )
        implementation = SessionEventIdentity.of(
            _session(
                tmp_path / "b",
                prompt_path,
                worktree_name="issue-orchestrator-6410",
                branch_name="6410-fix-the-thing",
                scratch_worktree=False,
            )
        )

        assert investigation.issue_number == implementation.issue_number
        assert investigation.session_id == implementation.session_id
        assert investigation.task == implementation.task
        assert investigation.timeline_actor is not implementation.timeline_actor
