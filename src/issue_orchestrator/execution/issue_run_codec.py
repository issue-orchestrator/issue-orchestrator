"""One codec owns SQLite run rows, storage comparison and column ordering."""

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.issue_key import GitHubIssueKey
from ..domain.issue_run_evidence import IssueRunEvidenceUnavailable, IssueRunRecord, RunTerminalBinding
from ..domain.registered_completion import CompletionRunRole
from ..domain.session_key import SessionKey, TaskKind
from ..domain.session_run import SessionRunAssets


@dataclass(frozen=True, slots=True)
class IssueRunRow:
    data: Mapping[str, Any]

    @classmethod
    def from_record(cls, issue_number: int, record: IssueRunRecord) -> "IssueRunRow":
        run = record.run
        # The storage boundary refuses an empty scope rather than persisting it.
        # `issue_scope TEXT NOT NULL` is satisfied by "", so the column alone never
        # caught this: the poisoned row outlived the launch and only failed at
        # teardown, where recovery is impossible (#7255).
        try:
            scope = record.session_key.issue.scope()
        except ValueError as exc:
            # `GitHubIssueKey.scope()` raises ValueError, and this codec is built
            # OUTSIDE `record_run`'s try while `WorktreeContext.create` catches
            # only IssueRunEvidenceUnavailable -- so an unwrapped ValueError
            # escapes unhandled AFTER the worktree and branch exist.
            raise IssueRunEvidenceUnavailable(
                f"run {run.run_id} for issue #{issue_number} has no repository "
                f"scope and cannot be terminalized: {exc}"
            ) from exc
        if not scope.strip():
            # IssueRunEvidenceUnavailable, not ValueError: `record_run` builds
            # this codec OUTSIDE its try, and `WorktreeContext.create` catches
            # only this type. A bare ValueError here escapes unhandled AFTER the
            # worktree and branch have been created.
            raise IssueRunEvidenceUnavailable(
                f"run {run.run_id} for issue #{issue_number} has an empty issue "
                "scope; a run with no repository scope cannot be terminalized"
            )
        return cls(
            {
                "session_name": run.session_name,
                "run_id": run.run_id,
                "started_at": run.started_at,
                "issue_number": issue_number,
                "issue_scope": scope,
                "issue_key": record.session_key.issue.stable_id(),
                "task": record.session_key.task.value,
                "assets_json": json.dumps(
                    run.to_dict(), sort_keys=True, separators=(",", ":")
                ),
                "branch_name": record.branch_name,
                "terminal_binding": None if record.terminal_binding is None else json.dumps({"terminal_id": record.terminal_binding.terminal_id}),
                "agent_label": record.agent_label,
                "completion_task": record.completion_task.value
                if record.completion_task is not None
                else None,
                # Retain the allocated lexical path without following mutable targets.
                "run_dir": os.path.normpath(str(run.run_dir)),
                "recorded_at": record.recorded_at,
            }
        )

    @property
    def select_sql(self) -> str:
        return "SELECT * FROM issue_runs WHERE session_name=? AND run_id=? AND started_at=?"

    @property
    def key_values(self) -> tuple[object, ...]:
        return tuple(
            self.data[column] for column in ("session_name", "run_id", "started_at")
        )

    @property
    def insert_sql(self) -> str:
        return """INSERT INTO issue_runs (
            session_name,run_id,started_at,issue_number,issue_scope,issue_key,
            task,assets_json,branch_name,terminal_binding,agent_label,completion_task,run_dir,recorded_at
        ) VALUES (
            :session_name,:run_id,:started_at,:issue_number,:issue_scope,:issue_key,
            :task,:assets_json,:branch_name,:terminal_binding,:agent_label,:completion_task,:run_dir,:recorded_at
        )"""

    @property
    def insert_values(self) -> dict[str, Any]:
        return dict(self.data)

    def require_existing(self, row: sqlite3.Row) -> None:
        expected = dict(self.data)
        # Retrying registration retains the first observation time.
        expected["recorded_at"] = row["recorded_at"]
        if any(row[column] != value for column, value in expected.items()):
            raise IssueRunEvidenceUnavailable(
                f"Conflicting ownership for run {self.key_values}"
            )

    def processing_role(self) -> CompletionRunRole:
        record = self.decode()
        return CompletionRunRole.from_recorded(
            self.data["issue_number"], record.completion_task, record.agent_label
        )

    def decode(self) -> IssueRunRecord:
        row = self.data
        payload = json.loads(row["assets_json"])
        if not isinstance(payload, dict):
            raise ValueError("Run ledger assets must be an object")
        assets = SessionRunAssets.from_dict(payload)
        if (assets.session_name, assets.run_id, assets.started_at) != self.key_values:
            raise ValueError("Run ledger key and assets disagree")
        if os.path.normpath(str(assets.run_dir)) != row["run_dir"]:
            raise ValueError("Run ledger root and assets disagree")
        # Refuse a row written before the write guard existed, at the same
        # boundary. Without this an unscoped row decodes into a GitHubIssueKey
        # whose `scope()` now raises, and it would raise from wherever the record
        # eventually travelled - `SessionKey.__hash__`, `SessionKey.__eq__`,
        # `manual_completion_preparation` - instead of here. `recorded_runs`
        # already turns this into the typed `IssueRunEvidenceUnavailable` its
        # callers handle.
        if not str(row["issue_scope"]).strip():
            # Typed for BOTH read paths: `recorded_runs` and the unwrapped
            # `recorded_run` both contract on IssueRunEvidenceUnavailable.
            raise IssueRunEvidenceUnavailable(
                f"run {row['run_id']} for issue #{row['issue_number']} was "
                "recorded with no repository scope and cannot be terminalized"
            )
        return IssueRunRecord(
            session_key=SessionKey(
                GitHubIssueKey(repo=row["issue_scope"], external_id=row["issue_key"]),
                TaskKind(row["task"]),
            ),
            run=assets,
            recorded_at=row["recorded_at"],
            branch_name=row["branch_name"],
            terminal_binding=None if row["terminal_binding"] is None else RunTerminalBinding(**json.loads(row["terminal_binding"])),
            agent_label=row["agent_label"],
            completion_task=TaskKind(row["completion_task"])
            if row["completion_task"] is not None
            else None,
        )
