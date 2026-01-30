"""SQLite-backed store for Goal Pilot state."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..domain.goal_pilot import (
    GoalPilotAction,
    GoalPilotNote,
    GoalPilotRun,
    GoalPilotSkill,
    GoalPilotSnapshot,
)
from ..infra.repo_identity import state_dir


_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_pilot_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    name TEXT NOT NULL,
    goals_json TEXT NOT NULL,
    done_criteria_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal_pilot_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES goal_pilot_runs(run_id)
);

CREATE TABLE IF NOT EXISTS goal_pilot_actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    action_type TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES goal_pilot_runs(run_id)
);

CREATE TABLE IF NOT EXISTS goal_pilot_notes (
    note_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note_type TEXT NOT NULL,
    note_text TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES goal_pilot_runs(run_id)
);

CREATE TABLE IF NOT EXISTS goal_pilot_skills (
    skill_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- draft | active | deprecated
    title TEXT NOT NULL,
    intent TEXT NOT NULL,
    triggers_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    playbook TEXT NOT NULL,
    examples_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    last_verified TEXT
);

CREATE INDEX IF NOT EXISTS idx_goal_pilot_runs_status
    ON goal_pilot_runs(status);

CREATE INDEX IF NOT EXISTS idx_goal_pilot_actions_run_id
    ON goal_pilot_actions(run_id);

CREATE INDEX IF NOT EXISTS idx_goal_pilot_snapshots_run_id
    ON goal_pilot_snapshots(run_id);

CREATE INDEX IF NOT EXISTS idx_goal_pilot_skills_status
    ON goal_pilot_skills(status);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteGoalPilotStore:
    """Durable storage for Goal Pilot state."""

    def __init__(self, repo_root: Path | str, db_path: Path | None = None) -> None:
        self._db_path = db_path or (state_dir(repo_root) / "goal_pilot.sqlite")
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SCHEMA)
        self._ensure_run_name_not_null()

    def _ensure_run_name_not_null(self) -> None:
        """Ensure goal_pilot_runs.name is NOT NULL (migrates if needed)."""
        conn = self._get_connection()
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(goal_pilot_runs)").fetchall()}
        if not columns:
            return
        name_info = columns.get("name")
        if name_info is None:
            with self._transaction() as tx:
                tx.execute("ALTER TABLE goal_pilot_runs ADD COLUMN name TEXT")
                tx.execute("UPDATE goal_pilot_runs SET name = run_id WHERE name IS NULL OR name = ''")
            columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(goal_pilot_runs)").fetchall()}
            name_info = columns.get("name")
        if name_info and name_info["notnull"] == 1:
            return

        with self._transaction() as tx:
            tx.execute("UPDATE goal_pilot_runs SET name = run_id WHERE name IS NULL OR name = ''")
            tx.execute("""
                CREATE TABLE IF NOT EXISTS goal_pilot_runs_v2 (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    name TEXT NOT NULL,
                    goals_json TEXT NOT NULL,
                    done_criteria_json TEXT NOT NULL
                )
            """)
            tx.execute("""
                INSERT INTO goal_pilot_runs_v2 (
                    run_id, created_at, updated_at, status, name, goals_json, done_criteria_json
                )
                SELECT run_id, created_at, updated_at, status, name, goals_json, done_criteria_json
                FROM goal_pilot_runs
            """)
            tx.execute("DROP TABLE goal_pilot_runs")
            tx.execute("ALTER TABLE goal_pilot_runs_v2 RENAME TO goal_pilot_runs")
            tx.execute("""
                CREATE INDEX IF NOT EXISTS idx_goal_pilot_runs_status
                    ON goal_pilot_runs(status)
            """)

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn.execute("PRAGMA journal_mode = WAL")
        return self._local.conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._get_connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def create_run(
        self,
        goals: list[str],
        done_criteria: dict[str, Any],
        status: str = "active",
        run_id: str | None = None,
        name: str = "",
    ) -> GoalPilotRun:
        run_id = run_id or f"gpr-{uuid.uuid4().hex[:12]}"
        if not name or not str(name).strip():
            raise ValueError("GoalPilot run name is required")
        now = _now_iso()
        goals_json = json.dumps(goals)
        done_criteria_json = json.dumps(done_criteria)

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO goal_pilot_runs (
                    run_id, created_at, updated_at, status, name, goals_json, done_criteria_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, now, now, status, name, goals_json, done_criteria_json),
            )

        return GoalPilotRun(
            run_id=run_id,
            created_at=now,
            updated_at=now,
            status=status,
            name=name,
            goals=goals,
            done_criteria=done_criteria,
        )

    def get_run(self, run_id: str) -> GoalPilotRun | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM goal_pilot_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return GoalPilotRun(
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            name=row["name"],
            goals=json.loads(row["goals_json"]),
            done_criteria=json.loads(row["done_criteria_json"]),
        )

    def update_run_status(self, run_id: str, status: str) -> None:
        now = _now_iso()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE goal_pilot_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, now, run_id),
            )

    def update_run_goals(self, run_id: str, goals: list[str]) -> None:
        now = _now_iso()
        goals_json = json.dumps(goals)
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE goal_pilot_runs
                SET goals_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (goals_json, now, run_id),
            )

    def add_snapshot(
        self,
        run_id: str,
        source_hash: str,
        summary: dict[str, Any],
        snapshot_id: str | None = None,
    ) -> GoalPilotSnapshot:
        snapshot_id = snapshot_id or f"gps-{uuid.uuid4().hex[:12]}"
        created_at = _now_iso()
        summary_json = json.dumps(summary)

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO goal_pilot_snapshots (
                    snapshot_id, run_id, created_at, source_hash, summary_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_id, run_id, created_at, source_hash, summary_json),
            )

        return GoalPilotSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            created_at=created_at,
            source_hash=source_hash,
            summary=summary,
        )

    def get_latest_snapshot(self, run_id: str) -> GoalPilotSnapshot | None:
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT * FROM goal_pilot_snapshots
            WHERE run_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return GoalPilotSnapshot(
            snapshot_id=row["snapshot_id"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            source_hash=row["source_hash"],
            summary=json.loads(row["summary_json"]),
        )

    def add_action(
        self,
        run_id: str,
        action_type: str,
        input_data: dict[str, Any],
        result_data: dict[str, Any],
        status: str,
        action_id: str | None = None,
    ) -> GoalPilotAction:
        action_id = action_id or f"gpa-{uuid.uuid4().hex[:12]}"
        created_at = _now_iso()
        input_json = json.dumps(input_data)
        result_json = json.dumps(result_data)

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO goal_pilot_actions (
                    action_id, run_id, created_at, action_type, input_json, result_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (action_id, run_id, created_at, action_type, input_json, result_json, status),
            )

        return GoalPilotAction(
            action_id=action_id,
            run_id=run_id,
            created_at=created_at,
            action_type=action_type,
            input_data=input_data,
            result_data=result_data,
            status=status,
        )

    def list_actions(self, run_id: str) -> list[GoalPilotAction]:
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT * FROM goal_pilot_actions
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            GoalPilotAction(
                action_id=row["action_id"],
                run_id=row["run_id"],
                created_at=row["created_at"],
                action_type=row["action_type"],
                input_data=json.loads(row["input_json"]),
                result_data=json.loads(row["result_json"]),
                status=row["status"],
            )
            for row in rows
        ]

    def add_note(
        self,
        run_id: str,
        note_type: str,
        note_text: str,
        note_id: str | None = None,
    ) -> GoalPilotNote:
        note_id = note_id or f"gpn-{uuid.uuid4().hex[:12]}"
        created_at = _now_iso()

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO goal_pilot_notes (
                    note_id, run_id, created_at, note_type, note_text
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (note_id, run_id, created_at, note_type, note_text),
            )

        return GoalPilotNote(
            note_id=note_id,
            run_id=run_id,
            created_at=created_at,
            note_type=note_type,
            note_text=note_text,
        )

    def list_notes(self, run_id: str, note_type: str | None = None) -> list[GoalPilotNote]:
        conn = self._get_connection()
        if note_type is None:
            rows = conn.execute(
                """
                SELECT * FROM goal_pilot_notes
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM goal_pilot_notes
                WHERE run_id = ? AND note_type = ?
                ORDER BY created_at ASC
                """,
                (run_id, note_type),
            ).fetchall()
        return [
            GoalPilotNote(
                note_id=row["note_id"],
                run_id=row["run_id"],
                created_at=row["created_at"],
                note_type=row["note_type"],
                note_text=row["note_text"],
            )
            for row in rows
        ]

    def upsert_skill(
        self,
        title: str,
        intent: str,
        triggers: list[str],
        constraints: list[str],
        playbook: str,
        examples: list[str],
        sources: list[str],
        status: str = "draft",
        skill_id: str | None = None,
        last_verified: str | None = None,
    ) -> GoalPilotSkill:
        skill_id = skill_id or f"gpsk-{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        triggers_json = json.dumps(triggers)
        constraints_json = json.dumps(constraints)
        examples_json = json.dumps(examples)
        sources_json = json.dumps(sources)

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO goal_pilot_skills (
                    skill_id, created_at, updated_at, status, title, intent,
                    triggers_json, constraints_json, playbook, examples_json,
                    sources_json, last_verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    title=excluded.title,
                    intent=excluded.intent,
                    triggers_json=excluded.triggers_json,
                    constraints_json=excluded.constraints_json,
                    playbook=excluded.playbook,
                    examples_json=excluded.examples_json,
                    sources_json=excluded.sources_json,
                    last_verified=excluded.last_verified
                """,
                (
                    skill_id,
                    now,
                    now,
                    status,
                    title,
                    intent,
                    triggers_json,
                    constraints_json,
                    playbook,
                    examples_json,
                    sources_json,
                    last_verified,
                ),
            )

        return GoalPilotSkill(
            skill_id=skill_id,
            created_at=now,
            updated_at=now,
            status=status,
            title=title,
            intent=intent,
            triggers=triggers,
            constraints=constraints,
            playbook=playbook,
            examples=examples,
            sources=sources,
            last_verified=last_verified,
        )

    def list_skills(self, status: str | None = None) -> list[GoalPilotSkill]:
        conn = self._get_connection()
        if status is None:
            rows = conn.execute(
                """
                SELECT * FROM goal_pilot_skills
                ORDER BY updated_at DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM goal_pilot_skills
                WHERE status = ?
                ORDER BY updated_at DESC
                """,
                (status,),
            ).fetchall()
        return [self._row_to_skill(row) for row in rows]

    def get_skill(self, skill_id: str) -> GoalPilotSkill | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM goal_pilot_skills WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_skill(row)

    def _row_to_skill(self, row: sqlite3.Row) -> GoalPilotSkill:
        return GoalPilotSkill(
            skill_id=row["skill_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            title=row["title"],
            intent=row["intent"],
            triggers=json.loads(row["triggers_json"]),
            constraints=json.loads(row["constraints_json"]),
            playbook=row["playbook"],
            examples=json.loads(row["examples_json"]),
            sources=json.loads(row["sources_json"]),
            last_verified=row["last_verified"],
        )
