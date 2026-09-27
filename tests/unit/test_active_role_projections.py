"""Every active-run projection names the run's recorded role (#7347 review r2).

A restored session's agent role comes back from the run ledger; the issue's
FIRST agent label is not the role (a failure investigation runs on its focus
issue, which still carries the coder's label). The dashboard, the board
snapshot, completion history and the control API status must say the same thing
the completion intake does.
"""

from datetime import datetime
from pathlib import Path

from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.entrypoints.control_api import _active_session_status_payload
from issue_orchestrator.view_models.dashboard import _build_active_items
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.test_completion_handler import make_handler


def _investigation(tmp_path: Path) -> Session:
    """A tech-lead run on a focus issue whose first agent label is the coder's."""
    return Session(
        key=SessionKey(FakeIssueKey("42"), SessionKind.TECH_LEAD),
        issue=Issue(42, "Focus", labels=["agent:web", "agent:tech-lead"]),
        agent_config=AgentConfig(prompt_path=tmp_path / "p.md"),
        terminal_id="issue-42",
        worktree_path=tmp_path,
        branch_name="tech-lead-investigation-42",
        run_assets=make_session_run_assets(tmp_path, session_name="coding-1"),
        agent_label="agent:tech-lead",
    )


def test_the_control_api_status_names_the_recorded_role(tmp_path):
    assert _active_session_status_payload(_investigation(tmp_path))["agent_type"] == "agent:tech-lead"


def test_completion_history_names_the_recorded_role(sample_config, tmp_path):
    entry = make_handler(sample_config)._create_history_entry(
        _investigation(tmp_path), SessionStatus.COMPLETED, None
    )
    assert entry.agent_type == "agent:tech-lead"


def test_the_dashboard_names_the_recorded_role(sample_config, tmp_path):
    state = OrchestratorState(active_sessions=[_investigation(tmp_path)])
    [item], _ = _build_active_items(state, sample_config, 1, set(), lm=LabelManager(sample_config))
    assert item["agent_type"] == "tech-lead"


def test_the_board_snapshot_names_the_recorded_role(tmp_path):
    builder = BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: [],
        log_tail_provider=lambda lines: [],
        case_file_reader=lambda: (),
        shipped_fix_reader=lambda limit: (),
        e2e_health_reader=lambda now: None,
        tech_lead_write_health_reader=lambda now: None,
        session_activity_reader=lambda session: None,
        clock=lambda: datetime(2026, 9, 27, 12, 0, 0),
    )
    info = builder._session_info(_investigation(tmp_path), datetime(2026, 9, 27, 12, 0, 0))
    assert info.agent_type == "agent:tech-lead"
