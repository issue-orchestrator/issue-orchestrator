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
        return cls(
            {
                "session_name": run.session_name,
                "run_id": run.run_id,
                "started_at": run.started_at,
                "issue_number": issue_number,
                "issue_scope": record.session_key.issue.scope(),
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
