from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.control.actions import (
    AddLabelAction,
    RemoveLabelAction,
    SupersedePullRequestAction,
)
from issue_orchestrator.control.completion_types import ProcessingResult
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publish_recovery import PublishRecoveryService
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.execution.json_publish_retry_locator_store import (
    JsonPublishRetryLocatorStore,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.background_job import CompletedJob
from issue_orchestrator.ports.pull_request_tracker import PRInfo

BRANCH = "4057-scratch-1"
PR_URL = "https://github.com/owner/repo/pull/5453"


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path / "repo"
    return config


@dataclass
class _Repo:
    issue: Issue
    labels: list[str]
    prs: list[PRInfo] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    superseded: list[int] = field(default_factory=list)

    def get_issue(self, issue_number: int) -> Issue | None:
        return self.issue if self.issue.number == issue_number else None

    def read_issue_labels(self, issue_number: int) -> list[str]:
        if self.issue.number != issue_number:
            return []
        return list(self.labels)

    def add_label(self, issue_number: int, label: str) -> None:
        if self.issue.number != issue_number:
            return
        if label not in self.labels:
            self.labels.append(label)
        self.added.append(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        if self.issue.number != issue_number:
            return
        self.labels = [current for current in self.labels if current != label]
        self.removed.append(label)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        if self.issue.number != issue_number:
            return []
        if state == "all":
            return list(self.prs)
        return [pr for pr in self.prs if pr.state == state]


@dataclass
class _CompletionProcessor:
    result: ProcessingResult
    calls: list[dict] = field(default_factory=list)

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: SessionRunAssets,
        pr_number: int | None = None,
        completion_path: str | None = None,
        agent_label: str | None = None,
    ) -> ProcessingResult:
        self.calls.append({
            "worktree": worktree,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "completion_path": completion_path,
            "agent_label": agent_label,
        })
        return self.result


class _RecordingRunner:
    """A BackgroundJobRunner that captures submitted work and runs it on demand.

    Keeps the republish off the request thread (``retry_publish`` only submits;
    the work executes when the test calls ``run_all``), then reports completions
    through ``drain_completed`` exactly like the production thread runner.
    """

    def __init__(self) -> None:
        self._pending: dict[str, Callable[[], None]] = {}
        self._running: set[str] = set()
        self._done: list[CompletedJob] = []

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        if job_id in self._running:
            return False
        self._running.add(job_id)
        self._pending[job_id] = fn
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self._running

    def run_all(self) -> None:
        for job_id, fn in list(self._pending.items()):
            error: BaseException | None = None
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 — mirror thread runner
                error = exc
            self._done.append(CompletedJob(job_id=job_id, error=error))
            self._pending.pop(job_id, None)
            self._running.discard(job_id)

    def drain_completed(self) -> list[CompletedJob]:
        done = self._done
        self._done = []
        return done


@dataclass
class _ActionApplier:
    repo: _Repo

    def apply(
        self,
        action: AddLabelAction | RemoveLabelAction | SupersedePullRequestAction,
    ) -> SimpleNamespace:
        if isinstance(action, AddLabelAction):
            self.repo.add_label(action.issue_number, action.label)
        elif isinstance(action, SupersedePullRequestAction):
            self.repo.superseded.append(action.pr_number)
            self.repo.prs = [
                pr for pr in self.repo.prs if pr.number != action.pr_number
            ]
        else:
            self.repo.remove_label(action.issue_number, action.label)
        return SimpleNamespace(success=True, error=None)


def _service(
    tmp_path: Path,
    repo: _Repo,
    lm: LabelManager,
    *,
    result: ProcessingResult | None = None,
    runner: _RecordingRunner | None = None,
) -> tuple[PublishRecoveryService, JsonPublishRetryLocatorStore, _RecordingRunner]:
    store = JsonPublishRetryLocatorStore(tmp_path / "publish_retry_locators.json")
    runner = runner or _RecordingRunner()
    processor = _CompletionProcessor(
        result or ProcessingResult(success=True, message="ok", pr_url=PR_URL)
    )
    service = PublishRecoveryService(
        repository_host=repo,
        completion_processor=processor,
        locator_store=store,
        runner=runner,
        label_manager=lm,
        fresh_issue_reader=repo,
        action_applier=_ActionApplier(repo),
    )
    return service, store, runner


def _issue(lm: LabelManager, *, publish_failed: bool = True) -> Issue:
    labels = ["agent:web"]
    if publish_failed:
        labels += [lm.publish_failed, lm.publish_fail_count_label(2)]
    return Issue(number=4057, title="UI: Surface provider status", labels=labels)


def _record_failure(
    service: PublishRecoveryService,
    make_session,
    tmp_path: Path,
) -> Session:
    """Persist retry locators the way the live completion path does."""
    session = make_session(
        issue_number=4057,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
    )
    completion_file = session.worktree_path / session.completion_path
    completion_file.parent.mkdir(parents=True, exist_ok=True)
    completion_file.write_text("{}")
    service.record_publish_failure(session, ["push_branch: Push failed: remote rejected"])
    return session


# ---------------------------------------------------------------------------
# Command surface A: publish-fail -> persisted retry locators
# ---------------------------------------------------------------------------


def test_record_publish_failure_persists_retry_locators(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, _ = _service(tmp_path, repo, lm)

    session = _record_failure(service, make_session, tmp_path)

    locators = store.get(4057)
    assert locators is not None
    assert locators.branch_name == BRANCH
    assert locators.worktree_path == str(session.worktree_path)
    assert locators.completion_path == session.completion_path


def test_record_publish_failure_ignores_non_publish_errors(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, _ = _service(tmp_path, repo, lm)

    session = make_session(issue_number=4057, branch_name=BRANCH)
    service.record_publish_failure(session, ["validation_failed: tests failed"])

    assert store.get(4057) is None


# ---------------------------------------------------------------------------
# Command surface B: retry request -> runner submission + drain reconciliation
# ---------------------------------------------------------------------------


def test_retry_publish_submits_off_thread_without_running_inline(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    result = service.retry_publish(4057, state)

    assert result.status == "submitted"
    assert result.job_id == "republish:4057"
    # The heavy publish work runs on the runner, not on the request thread.
    assert runner.is_running("republish:4057")


def test_retry_publish_reconciles_success_on_drain(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    assert lm.publish_failed in repo.removed
    assert lm.publish_fail_count_label(2) in repo.removed
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None  # locators cleared on success
    assert state.session_history[-1].status == "completed"
    assert state.session_history[-1].pr_url == PR_URL


def test_retry_publish_failure_leaves_issue_retryable(make_session, tmp_path) -> None:
    """A failed republish must not permanently lock out a second retry."""
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(
        tmp_path, repo, lm,
        result=ProcessingResult(success=False, message="push failed again"),
    )
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"
    # While running, a second retry is rejected by the live-runner in-flight guard.
    assert service.retry_publish(4057, state).status == "rejected"

    runner.run_all()
    service.drain_completed_retries(state)

    # publish-failed + locators survive a failed republish, so retry is available again.
    assert store.get(4057) is not None
    assert not runner.is_running("republish:4057")
    assert service.retry_publish(4057, state).status == "submitted"


# ---------------------------------------------------------------------------
# Recovery of an already-created PR
# ---------------------------------------------------------------------------


def test_retry_publish_recovers_existing_pr_and_clears_state(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    issue = _issue(lm)
    repo = _Repo(
        issue=issue,
        labels=list(issue.labels),
        prs=[
            PRInfo(
                number=5453,
                title="#4057: UI: Surface provider status",
                url=PR_URL,
                branch=BRANCH,
                body="",
                state="open",
                labels=[],
            )
        ],
    )
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState(
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title=issue.title,
                agent_type="agent:web",
                status="failed",
                runtime_minutes=5,
                status_reason="Push or PR creation failed",
                completed_at=datetime.now(),
            )
        ]
    )

    result = service.retry_publish(4057, state)

    assert result.status == "recovered_existing_pr"
    assert not runner.is_running("republish:4057")  # no republish submitted
    assert store.get(4057) is None
    assert lm.publish_failed in repo.removed
    assert lm.pr_pending in repo.added
    assert not any(
        entry.issue_number == 4057 and entry.status in {"blocked", "failed"}
        for entry in state.session_history
    )
    assert state.session_history[-1].status == "completed"
    assert state.session_history[-1].pr_url == PR_URL


def test_retry_publish_rejected_without_locators(make_session, tmp_path) -> None:
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, _, _ = _service(tmp_path, repo, lm)

    result = service.retry_publish(4057, OrchestratorState())

    assert result.status == "rejected"
    assert "locators" in result.message.lower()


# ---------------------------------------------------------------------------
# Termination: abandon in-flight retry on reset (F1/A1 regression)
# ---------------------------------------------------------------------------


def test_abandon_issue_supersedes_late_retry_and_skips_reconcile(
    make_session, tmp_path
) -> None:
    """Reset while a republish is in flight must not repopulate the attempt.

    Start a retry, abandon the issue (as reset does), then drain a *successful*
    late retry result. It must supersede the PR the late worker created and
    must NOT add pr-pending, remove publish-failed, append completed history,
    or leave an unsuperseded stale PR.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    assert service.retry_publish(4057, state).status == "submitted"

    # Operator resets the issue before the republish thread drains.
    service.abandon_issue(4057)
    assert store.get(4057) is None  # durable locators dropped by abandon

    # The abandoned worker still finishes and lands a PR past reset's scan.
    repo.prs = [
        PRInfo(
            number=5453,
            title="#4057: UI: Surface provider status",
            url=PR_URL,
            branch=BRANCH,
            body="",
            state="open",
            labels=[],
        )
    ]
    runner.run_all()
    service.drain_completed_retries(state)

    # Late PR is superseded, not adopted as the issue's live attempt.
    assert repo.superseded == [5453]
    assert lm.pr_pending not in repo.added
    assert lm.publish_failed not in repo.removed
    assert not any(entry.issue_number == 4057 for entry in state.session_history)
    assert 4057 not in state.completed_today
    assert store.get(4057) is None


def test_abandon_issue_without_inflight_does_not_block_a_later_retry(
    make_session, tmp_path
) -> None:
    """Abandon with no in-flight work only clears locators, leaves no tombstone.

    A stale tombstone would silently drop the next legitimate retry, so prove a
    fresh retry after abandon still reconciles normally.
    """
    lm = LabelManager(_config(tmp_path))
    repo = _Repo(issue=_issue(lm), labels=list(_issue(lm).labels))
    service, store, runner = _service(tmp_path, repo, lm)
    _record_failure(service, make_session, tmp_path)
    state = OrchestratorState()

    service.abandon_issue(4057)
    assert store.get(4057) is None

    # A fresh attempt fails publish again and is retried.
    _record_failure(service, make_session, tmp_path)
    assert service.retry_publish(4057, state).status == "submitted"
    runner.run_all()
    service.drain_completed_retries(state)

    # Reconciled as a normal success — the earlier abandon left no tombstone.
    assert lm.publish_failed in repo.removed
    assert lm.pr_pending in repo.added
    assert store.get(4057) is None
    assert state.session_history[-1].status == "completed"
    assert repo.superseded == []
