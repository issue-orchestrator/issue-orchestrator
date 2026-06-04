from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.control.actions import AddLabelAction, RemoveLabelAction
from issue_orchestrator.control.job_store import set_worktree_id, JobRecord
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publish_recovery import PublishRecoveryService
from issue_orchestrator.domain.models import Issue, OrchestratorState, PublishJob, SessionHistoryEntry
from issue_orchestrator.infra.config import Config
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.pull_request_tracker import PRInfo


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

    def get_issue(self, issue_number: int) -> Issue | None:
        return self.issue if self.issue.number == issue_number else None

    def get_issue_labels(self, issue_number: int) -> list[str]:
        if self.issue.number != issue_number:
            return []
        return list(self.labels)

    def read_issue_labels(self, issue_number: int) -> list[str]:
        return self.get_issue_labels(issue_number)

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
class _Executor:
    jobs: list[JobRecord]
    running: list[PublishJob] = field(default_factory=list)
    submitted: list[PublishJob] = field(default_factory=list)

    def submit(self, job: PublishJob) -> bool:
        self.submitted.append(job)
        return True

    def get_job_history(self, issue_number: int | None = None, limit: int = 100) -> list[JobRecord]:
        if issue_number is None:
            return list(self.jobs)[:limit]
        return [job for job in self.jobs if job.issue_number == issue_number][:limit]

    def get_running_jobs(self) -> list[PublishJob]:
        return list(self.running)


@dataclass
class _ActionApplier:
    repo: _Repo

    def apply(self, action: AddLabelAction | RemoveLabelAction) -> SimpleNamespace:
        if isinstance(action, AddLabelAction):
            self.repo.add_label(action.issue_number, action.label)
        else:
            self.repo.remove_label(action.issue_number, action.label)
        return SimpleNamespace(success=True, error=None)


def _failed_job(tmp_path: Path, *, issue_number: int = 4057, branch: str = "4057-scratch-1") -> JobRecord:
    worktree = tmp_path / "worktree-4057"
    worktree.mkdir(parents=True)
    worktree_id = set_worktree_id(worktree, "wt-4057")
    session_output = FileSystemSessionOutput()
    run_assets = session_output.start_run(
        worktree,
        "coding-1",
        issue_number=issue_number,
        agent_label="agent:web",
        backend="subprocess",
    )
    completion_rel = (
        f".issue-orchestrator/sessions/{run_assets.run_dir.name}/"
        "completion-agent-web.json"
    )
    completion_path = worktree / completion_rel
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text("{}")
    metadata = json.dumps({
        "issue_title": "UI: Surface provider circuit breaker status",
        "outcome": "completed",
        "requested_actions": ["push_branch", "create_pr"],
        "completion_path": completion_rel,
        "agent_label": "agent:web",
        "run_assets": run_assets.to_dict(),
    })
    return JobRecord(
        job_id="job-failed-1",
        issue_number=issue_number,
        session_key="coding-1",
        worktree_path=str(worktree),
        worktree_id=worktree_id,
        branch_name=branch,
        status="failed",
        created_at=10.0,
        started_at=11.0,
        finished_at=12.0,
        error_message="push failed",
        metadata_json=metadata,
    )


def test_retry_publish_submits_manual_publish_job(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lm = LabelManager(config)
    issue = Issue(
        number=4057,
        title="UI: Surface provider circuit breaker status",
        labels=["agent:web", lm.publish_failed, lm.publish_fail_count_label(2)],
    )
    repo = _Repo(issue=issue, labels=list(issue.labels))
    executor = _Executor(jobs=[_failed_job(tmp_path)])
    service = PublishRecoveryService(repo, executor, lm, repo, _ActionApplier(repo))
    state = OrchestratorState()

    result = service.retry_publish(4057, state)

    assert result.status == "submitted"
    assert len(executor.submitted) == 1
    submitted = executor.submitted[0]
    assert submitted.retry_publish is True
    assert submitted.branch_name == "4057-scratch-1"
    assert submitted.issue_number == 4057
    assert submitted.job_id in state.pending_publish_jobs


def test_retry_publish_recovers_existing_pr_and_clears_publish_failed_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lm = LabelManager(config)
    issue = Issue(
        number=4057,
        title="UI: Surface provider circuit breaker status",
        labels=["agent:web", lm.publish_failed, lm.publish_fail_count_label(2)],
    )
    repo = _Repo(
        issue=issue,
        labels=list(issue.labels),
        prs=[
            PRInfo(
                number=5453,
                title="#4057: UI: Surface provider circuit breaker status",
                url="https://github.com/owner/repo/pull/5453",
                branch="4057-scratch-1",
                body="",
                state="open",
                labels=[],
            )
        ],
    )
    executor = _Executor(jobs=[_failed_job(tmp_path)])
    service = PublishRecoveryService(repo, executor, lm, repo, _ActionApplier(repo))
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
    assert lm.publish_failed in repo.removed
    assert lm.publish_fail_count_label(2) in repo.removed
    assert lm.pr_pending in repo.added
    assert not any(
        entry.issue_number == 4057 and entry.status in {"blocked", "failed"}
        for entry in state.session_history
    )
    assert state.session_history[-1].status == "completed"
    assert state.session_history[-1].pr_url == "https://github.com/owner/repo/pull/5453"
