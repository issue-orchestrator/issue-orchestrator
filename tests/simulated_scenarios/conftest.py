from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

import pytest

from issue_orchestrator.domain.models import Issue, AgentConfig
from issue_orchestrator.ports.worktree_manager import WorktreeInfo
from issue_orchestrator.infra.config import Config
from tests.conftest import MockGitHubAdapter, MockEventSink, build_test_orchestrator_deps
from issue_orchestrator.execution import CompositeEventSink
from issue_orchestrator.execution.timeline_event_sink import TimelineEventSink
from issue_orchestrator.execution.timeline_store import FileSystemTimelineStore, TimelineStoreConfig
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.infra.orchestrator import Orchestrator
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.workflows.review_workflow import ReviewWorkflow
from issue_orchestrator.control.workflows.rework_workflow import ReworkWorkflow


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tests" / "simulated_scenarios" / "fixtures" / "scripts"


class ScriptSessionRunner:
    """SessionRunner that executes commands synchronously via bash."""

    def __init__(self) -> None:
        self._last_output: dict[str, str] = {}

    def create_session(self, session_id: int, command: str, working_dir: str, title: str | None, session_name: str) -> bool:
        # Ensure fixture scripts that invoke `python` use the same interpreter
        # as the test process (e.g., .venv/bin/python in CI).
        env = dict(os.environ)
        python_bin_dir = str(Path(sys.executable).parent)
        env["PATH"] = f"{python_bin_dir}:{env.get('PATH', '')}"
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_dir,
            executable="/bin/bash",
            env=env,
            capture_output=True,
            text=True,
        )
        self._last_output[session_name] = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return False

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def kill_session(self, session_id: int, session_name: str) -> None:
        return None

    def discover_running_sessions(self) -> list[dict]:
        return []

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        return self._last_output.get(session_name)

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        return None

    def on_orchestrator_shutdown(self) -> None:
        return None


@dataclass
class StubWorkingCopy:
    branch: str = "issue-1"

    def get_head_sha(self, worktree: Path) -> str | None:
        return "deadbeef"

    def get_current_branch(self, worktree: Path) -> str | None:
        return self.branch

    def rebase_on_branch(self, worktree: Path, target: str = "origin/main"):
        return None

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        self.branch = branch

    def list_branch_names(self, worktree: Path) -> list[str]:
        return [self.branch]

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False

    def push(self, worktree: Path, remote: str = "origin", force_with_lease: bool = True, set_upstream: bool = True, skip_hooks: bool = False):
        return type("PushResult", (), {"success": True, "message": "ok"})()

    def default_branch(self, repo_root: Path, remote: str = "origin") -> str:
        return "main"


class TempWorktreeManager:
    def __init__(self, base: Path) -> None:
        self.base = base

    def create(self, repo_root: Path, issue_number: int, issue_title: str, worktree_base: Path | None = None,
               enforce_hooks: bool = True, pre_push_hook: Path | None = None, branch_name: str | None = None,
               base_branch: str | None = None, reuse_options=None):
        worktree = (worktree_base or self.base) / f"sim-wt-{issue_number}"
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text("simulated")
        return WorktreeInfo(path=worktree, branch_name=branch_name or f"{issue_number}-sim")

    def remove(self, worktree_path: Path) -> None:
        return None

    def extract_issue_number(self, branch_name: str) -> int | None:
        parts = branch_name.split("-")
        return int(parts[0]) if parts and parts[0].isdigit() else None


@pytest.fixture
def scenario_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    return repo_root


def build_config(
    repo_root: Path,
    *,
    coder_command: str,
    reviewer_command: str,
    review_exchange_mode: str = "via-local-loop",
    review_exchange_require_validation: bool = False,
    review_exchange_max_rounds: int = 5,
    review_exchange_max_no_progress: int = 2,
    validation_cmd: str | None = None,
    max_validation_retries: int = 0,
) -> Config:
    config = Config()
    config.repo_root = repo_root
    config.repo = "local/test"
    config.worktree_base = repo_root / ".issue-orchestrator" / "worktrees"
    config.worktree_base.mkdir(parents=True, exist_ok=True)
    config.queue_refresh_seconds = 0
    # Simulated scenarios advance quickly without wall-clock waits; use
    # immediate network sync to keep mocked PR/label transitions deterministic.
    config.fetch_layer_network_sync_seconds = 0
    config.max_concurrent_sessions = 1
    config.review_enabled = True
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = "needs-code-review"
    config.code_reviewed_label = "code-reviewed"
    config.review_exchange_mode = review_exchange_mode
    config.review_exchange_require_validation = review_exchange_require_validation
    config.review_exchange_max_rounds = review_exchange_max_rounds
    config.review_exchange_max_no_progress = review_exchange_max_no_progress
    config.filtering.label = "simulated-scenario"
    config.retry.max_validation_retries = max_validation_retries

    if validation_cmd:
        config.validation.cmd = validation_cmd
        config.validation.timeout_seconds = 5

    prompt = repo_root / "prompt.md"
    prompt.write_text("Simulated scenario prompt")

    config.agents = {
        "agent:coder": AgentConfig(
            prompt_path=prompt,
            model="test",
            timeout_minutes=1,
            command=coder_command,
            reviewer="agent:reviewer",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=prompt,
            model="test",
            timeout_minutes=1,
            command=reviewer_command,
        ),
    }
    return config


class FreshIssueReader:
    def __init__(self, labels_by_issue: dict[int, set[str]]) -> None:
        self._labels_by_issue = labels_by_issue

    def read_issue_labels(self, issue_number: int) -> list[str]:
        return sorted(self._labels_by_issue.get(issue_number, set()))


def build_orchestrator(
    repo_root: Path,
    issues: list[Issue],
    config: Config,
    *,
    repo_host: MockGitHubAdapter | None = None,
    events: MockEventSink | None = None,
    runner: ScriptSessionRunner | None = None,
    worktree_manager: TempWorktreeManager | None = None,
    working_copy: StubWorkingCopy | None = None,
    lease_renewer: object | None = None,
    reconcile: bool = False,
    fresh_labels: dict[int, set[str]] | None = None,
) -> tuple[Orchestrator, MockGitHubAdapter, MockEventSink, DefaultTimelineReader]:
    repo_host = repo_host or MockGitHubAdapter()
    repo_host.issues = issues

    events = events or MockEventSink()
    runner = runner or ScriptSessionRunner()
    worktree_manager = worktree_manager or TempWorktreeManager(base=repo_root)
    working_copy = working_copy or StubWorkingCopy()
    timeline_store = FileSystemTimelineStore(
        repo_root,
        TimelineStoreConfig(max_records=config.timeline.max_records),
    )
    timeline_writer = DefaultTimelineWriter(timeline_store)
    timeline_reader = DefaultTimelineReader(timeline_store)
    composite_events = CompositeEventSink(events, TimelineEventSink(timeline_writer))
    scheduler = Scheduler(config=config)
    planner = Planner(
        config=config,
        scheduler=scheduler,
        review_workflow=ReviewWorkflow(config=config, events=composite_events),
        rework_workflow=ReworkWorkflow(config=config, events=composite_events),
    )

    deps = build_test_orchestrator_deps(
        config,
        repo_host,
        composite_events,
        runner,
        worktree_manager,
        working_copy=working_copy,
        lease_renewer=lease_renewer,
        planner=planner,
        timeline_reader=timeline_reader,
        timeline_writer=timeline_writer,
    )

    if reconcile:
        deps.action_applier.reconcile = True
        labels_by_issue = fresh_labels or {}
        deps.action_applier.fresh_issue_reader = FreshIssueReader(labels_by_issue)

    orchestrator = Orchestrator(config=config, deps=deps)
    orchestrator.deps.completion_processor.set_event_emitter(
        composite_events,
        orchestrator.event_context,
    )
    return orchestrator, repo_host, events, timeline_reader


def run_until(orchestrator: Orchestrator, predicate, max_ticks: int = 10) -> None:
    for _ in range(max_ticks):
        orchestrator.tick()
        if predicate():
            return
    raise AssertionError("predicate not satisfied before max_ticks")


def run_until_event(orchestrator: Orchestrator, events: MockEventSink, name, max_ticks: int = 10) -> None:
    def _predicate() -> bool:
        return any(e.name == name for e in events.events)
    run_until(orchestrator, _predicate, max_ticks=max_ticks)


def run_until_pending_reviews(orchestrator: Orchestrator, expected: int, max_ticks: int = 10) -> None:
    def _predicate() -> bool:
        return len(orchestrator.state.pending_reviews) >= expected
    run_until(orchestrator, _predicate, max_ticks=max_ticks)
