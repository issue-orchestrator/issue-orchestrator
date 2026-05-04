from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from issue_orchestrator.domain.models import Issue, AgentConfig
from issue_orchestrator.execution.agent_runner import AgentRunner, AgentSpec
from issue_orchestrator.ports.working_copy import BranchStatus, CommitInfo, PreflightResult, PushResult, RebaseResult
from issue_orchestrator.ports.worktree_manager import WorktreeInfo
from issue_orchestrator.infra.config import Config
from tests.conftest import MockGitHubAdapter, MockEventSink, build_test_orchestrator_deps
from issue_orchestrator.execution import CompositeEventSink
from issue_orchestrator.execution.timeline_event_sink import TimelineEventSink
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore, TimelineStoreConfig
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.infra.orchestrator import Orchestrator
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.workflows.review_workflow import ReviewWorkflow
from issue_orchestrator.control.workflows.rework_workflow import ReworkWorkflow


@pytest.fixture(autouse=True)
def _strip_nested_session_env(monkeypatch):
    """Allow Claude subprocess invocations from within a Claude Code session.

    Claude Code sets CLAUDECODE and CLAUDE_CODE_ENTRYPOINT to detect nested
    launches. Strip them so integration tests that spawn Claude subprocesses
    work regardless of whether the test runner itself is a Claude Code agent.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.fixture(autouse=True)
def _stub_persistent_review_exchange_setup(monkeypatch):
    """Bypass the persistent-session runner in simulated scenarios.

    The simulated-scenario harness was built around the spawn-per-phase
    review-exchange model where each round was a fresh subprocess managed
    by ``ScriptSessionRunner``. The persistent runner manages its own
    subprocesses via PTY directly, so scenarios that exercise
    review-exchange would now hit real ``git rev-parse``, ``git worktree
    add``, and PTY spawns against non-git scratch dirs.

    Stub:
      * the reviewer-worktree git helpers (no real git repo needed)
      * the runner itself (no real PTY/subprocess work)

    The persistent runner's behavior is covered exhaustively by
    ``tests/unit/execution/test_persistent_session_exchange.py`` (20
    tests) and ``test_persistent_round_runner.py`` (22 tests). Migrating
    the simulated-scenario harness to drive the persistent runner
    natively is tracked as a follow-up.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from issue_orchestrator.domain.review_exchange import (
        ReviewExchangeOutcome,
        ReviewExchangeResponse,
    )
    from issue_orchestrator.events import EventName
    from issue_orchestrator.execution import (
        persistent_session_exchange as pse,
        reviewer_worktree as rw,
    )

    def _stub_create(*, coder_worktree, coder_branch, timestamp):  # noqa: ARG001
        sibling = coder_worktree.parent / f"{coder_worktree.name}-review-{timestamp}"
        sibling.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(path=sibling, coder_branch=coder_branch)

    monkeypatch.setattr(rw, "resolve_current_branch", lambda _wt: "issue-1")
    monkeypatch.setattr(rw, "create_reviewer_worktree", _stub_create)
    monkeypatch.setattr(rw, "fast_forward_reviewer_worktree", lambda _r: "deadbeef")
    monkeypatch.setattr(rw, "remove_reviewer_worktree", lambda *_, **__: None)

    def _stub_run_persistent_session_exchange(
        *,
        session_output,
        coder_worktree_path,
        reviewer_worktree_path,  # noqa: ARG001
        issue_number,
        issue_title,  # noqa: ARG001
        coder_label,  # noqa: ARG001
        reviewer_label,  # noqa: ARG001
        coder_agent,  # noqa: ARG001
        reviewer_agent,  # noqa: ARG001
        max_rounds,  # noqa: ARG001
        max_no_progress,  # noqa: ARG001
        require_validation,  # noqa: ARG001
        initial_validation_record_path=None,  # noqa: ARG001
        web_port=None,  # noqa: ARG001
        events=None,
        event_context=None,
        on_started=None,
        before_reviewer_round=None,  # noqa: ARG001
    ):
        """Produce the events + outcome the orchestration logic expects.

        Mirrors the shape of a single-round, reviewer-approved exchange
        without spawning anything. Scenarios that need a different
        outcome (changes_requested, no_progress, error) are skipped or
        will be migrated when the harness is updated.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        session_name = f"review-exchange-{issue_number}-{timestamp}"
        run = session_output.start_run(
            coder_worktree_path,
            session_name,
            issue_number=issue_number,
            agent_label=None,
            backend="persistent-pty",
        )
        run_dir = run.run_dir
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)

        if on_started is not None:
            on_started(run_dir)

        def _emit(name, payload):
            if events is None or event_context is None:
                return
            from issue_orchestrator.ports import make_trace_event
            enriched = dict(payload)
            enriched["run_dir"] = str(run_dir)
            enriched["session_run_id"] = run.run_id
            events.publish(make_trace_event(name, event_context.enrich(enriched)))

        _emit(EventName.REVIEW_EXCHANGE_STARTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "exchange_dir": str(exchange_dir),
        })
        reviewer = ReviewExchangeResponse(
            response_type="ok",
            response_text="LGTM (stubbed scenario response)",
            getting_closer=True,
        )
        _emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": 1,
            "reviewer_response_type": reviewer.response_type,
            "reviewer_response_text": reviewer.response_text,
            "coder_response_type": None,
        })
        _emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "rounds": 1,
            "status": "ok",
            "reason": "reviewer_ok",
        })
        summary = {
            "completed_rounds": 1,
            "status": "ok",
            "response_text": reviewer.response_text,
            "reason": "reviewer_ok",
        }
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="reviewer_ok",
            reviewer_response=reviewer,
            exchange_dir=exchange_dir,
            summary=summary,
        )

    monkeypatch.setattr(
        pse, "run_persistent_session_exchange", _stub_run_persistent_session_exchange,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tests" / "simulated_scenarios" / "fixtures" / "scripts"

_RUN_DIR_RE = re.compile(r"ISSUE_ORCHESTRATOR_RUN_DIR=(['\"]?)([^'\"\s]+)\1")


class ScriptSessionRunner:
    """SessionRunner that executes commands via the unified AgentRunner.

    Uses the same PTY → raw terminal recording chain as production. This
    ensures simulated scenario tests exercise the real capture path,
    preventing regressions like #4057.
    """

    def __init__(self) -> None:
        self._last_output: dict[str, str] = {}
        self._runner = AgentRunner()

    def create_session(self, session_id: int, command: str, working_dir: str, title: str | None, session_name: str) -> bool:
        python_bin_dir = str(Path(sys.executable).parent)

        # Extract run_dir from command to determine log/output paths.
        match = _RUN_DIR_RE.search(command)
        if match:
            run_dir = Path(match.group(2))
            if not run_dir.is_absolute():
                run_dir = (Path(working_dir) / run_dir).resolve()
        else:
            run_dir = Path(working_dir) / ".issue-orchestrator" / "sessions" / "fallback"

        spec = AgentSpec(
            command=["bash", "-c", command],
            working_dir=Path(working_dir),
            timeout_seconds=120,
            log_path=run_dir / "terminal-recording.jsonl",
            output_dir=run_dir,
            env_overrides={"PATH": f"{python_bin_dir}:{os.environ.get('PATH', '')}"},
        )
        result = self._runner.run(spec)

        # Populate _last_output from the canonical terminal recording for get_session_output().
        log_path = run_dir / "terminal-recording.jsonl"
        if log_path.exists():
            self._last_output[session_name] = _decode_terminal_recording(log_path)
        else:
            self._last_output[session_name] = ""

        return result.succeeded

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


class FastScriptSessionRunner:
    """Lightweight SessionRunner for state-machine scenarios.

    This bypasses the PTY/CleaningLogWriter path and runs the shell command
    directly, which is substantially cheaper for restart/recovery tests that
    only care about orchestrator behavior. Tests that verify ui-session.log
    filtering must opt into ``ScriptSessionRunner`` explicitly.
    """

    def __init__(self) -> None:
        self._last_output: dict[str, str] = {}

    def create_session(self, session_id: int, command: str, working_dir: str, title: str | None, session_name: str) -> bool:
        python_bin_dir = str(Path(sys.executable).parent)

        match = _RUN_DIR_RE.search(command)
        if match:
            run_dir = Path(match.group(2))
            if not run_dir.is_absolute():
                run_dir = (Path(working_dir) / run_dir).resolve()
        else:
            run_dir = Path(working_dir) / ".issue-orchestrator" / "sessions" / "fallback"
        run_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["PATH"] = f"{python_bin_dir}:{env.get('PATH', '')}"
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=working_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        output = f"{result.stdout}{result.stderr}"
        log_path = run_dir / "ui-session.log"
        log_path.write_text(output, encoding="utf-8", errors="replace")
        self._last_output[session_name] = output
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


def _decode_terminal_recording(path: Path) -> str:
    chunks: list[str] = []
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            chunks.append(raw_line)
            continue
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(chunks)


@dataclass
class StubWorkingCopy:
    """Stub implementing the WorkingCopy protocol for simulated scenario tests.

    Must stay in sync with ``ports/working_copy.py``.  Extra methods
    (``list_branch_names``, ``default_branch``) are kept because they satisfy
    the ``CompletionProcessor.GitAdapter`` protocol.
    """

    branch: str = "issue-1"

    def get_head_sha(self, worktree: Path) -> str | None:
        return "deadbeef"

    def get_current_branch(self, worktree: Path) -> str | None:
        return self.branch

    def get_branch_status(self, worktree: Path) -> BranchStatus | None:
        return BranchStatus(branch=self.branch, ahead=0, behind=0, has_remote=True, clean=True)

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False

    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool:
        return False

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        return []

    def fetch(self, worktree: Path, remote: str = "origin") -> bool:
        return True

    def list_remote_branches(self, repo_root: Path, remote: str = "origin") -> list[str]:
        return [f"{remote}/{self.branch}"]

    def get_commits_ahead_count(self, repo_root: Path, branch: str, base: str = "origin/main") -> int:
        return 0

    def get_last_commit_date(self, repo_root: Path, branch: str) -> str | None:
        return None

    def rebase_on_branch(self, worktree: Path, target: str = "origin/main") -> RebaseResult:
        return RebaseResult(success=True, message="ok")

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        self.branch = branch

    def push(self, worktree: Path, remote: str = "origin", force_with_lease: bool = True, set_upstream: bool = True, skip_hooks: bool = False) -> PushResult:
        return PushResult(success=True, branch=self.branch, remote=remote, message="ok")

    def get_issue_number_from_branch(self, worktree: Path) -> int | None:
        parts = self.branch.split("-")
        if parts and parts[0].isdigit():
            return int(parts[0])
        return None

    def push_preflight(self, worktree: Path, remote: str = "origin") -> PreflightResult:
        return PreflightResult(would_succeed=True)

    def delete_remote_branch(self, repo_root: Path, branch: str, remote: str = "origin") -> bool:
        return True

    # --- Extra methods for CompletionProcessor.GitAdapter protocol ---

    def list_branch_names(self, worktree: Path) -> list[str]:
        return [self.branch]

    def default_branch(self, repo_root: Path, remote: str = "origin") -> str:
        return "main"


class TempWorktreeManager:
    def __init__(self, base: Path) -> None:
        self.base = base

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options=None,
    ):
        worktree = (worktree_base or self.base) / f"sim-wt-{issue_number}"
        worktree.mkdir(parents=True, exist_ok=True)
        final_branch = branch_name or f"{issue_number}-sim"
        # Use a real git repo so branch/introspection commands work in scenarios.
        subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", final_branch], cwd=worktree, check=True, capture_output=True)
        return WorktreeInfo(path=worktree, branch_name=final_branch)

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
    # Use port 0 so the completion command's push preflight check cannot accidentally
    # connect to a real orchestrator running on the default port (8080).
    # Connection to port 0 always fails with URLError → preflight skipped.
    config.web_port = 0

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
    runner: ScriptSessionRunner | FastScriptSessionRunner | None = None,
    worktree_manager: TempWorktreeManager | None = None,
    working_copy: StubWorkingCopy | None = None,
    lease_renewer: object | None = None,
    reconcile: bool = False,
    fresh_labels: dict[int, set[str]] | None = None,
) -> tuple[Orchestrator, MockGitHubAdapter, MockEventSink, DefaultTimelineReader]:
    repo_host = repo_host or MockGitHubAdapter()
    repo_host.issues = issues

    events = events or MockEventSink()
    runner = runner or FastScriptSessionRunner()
    worktree_manager = worktree_manager or TempWorktreeManager(base=repo_root)
    working_copy = working_copy or StubWorkingCopy()
    timeline_store = SqliteTimelineStore(
        state_dir(repo_root) / "timeline.sqlite",
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
