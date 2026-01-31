"""Port for Goal Pilot persistence."""

from __future__ import annotations

from typing import Any, Protocol

from ..domain.goal_pilot import (
    GoalPilotAction,
    GoalPilotJourney,
    GoalPilotNote,
    GoalPilotPhaseChange,
    GoalPilotRun,
    GoalPilotSkill,
    GoalPilotSnapshot,
)


class GoalPilotStore(Protocol):
    """Protocol for Goal Pilot durable state."""

    def create_run(
        self,
        goals: list[str],
        done_criteria: dict[str, Any],
        status: str = "active",
        run_id: str | None = None,
        name: str = "",
        phase: str = "outcomes_opportunities",
    ) -> GoalPilotRun:
        ...

    def list_runs(self, limit: int = 50) -> list[GoalPilotRun]:
        ...

    def get_run(self, run_id: str) -> GoalPilotRun | None:
        ...

    def update_run_status(self, run_id: str, status: str) -> None:
        ...

    def update_run_phase(self, run_id: str, phase: str) -> None:
        ...

    def add_phase_change(
        self,
        run_id: str,
        from_phase: str,
        to_phase: str,
        reason: str,
        changes: dict[str, Any],
        phase_id: str | None = None,
    ) -> GoalPilotPhaseChange:
        ...

    def list_phase_history(self, run_id: str, limit: int = 50) -> list[GoalPilotPhaseChange]:
        ...

    def add_journey(
        self,
        run_id: str,
        title: str,
        description: str,
        order_index: int,
        priority: str,
        status: str,
        success_criteria: str,
        under_the_covers: dict[str, Any],
        lookahead: dict[str, Any],
        milestone: str | None = None,
        journey_id: str | None = None,
    ) -> GoalPilotJourney:
        ...

    def list_journeys(self, run_id: str) -> list[GoalPilotJourney]:
        ...

    def update_journey(self, journey_id: str, updates: dict[str, Any]) -> GoalPilotJourney:
        ...

    def reorder_journeys(self, run_id: str, ordered_ids: list[str]) -> None:
        ...

    def update_run_goals(self, run_id: str, goals: list[str]) -> None:
        ...

    def add_snapshot(
        self,
        run_id: str,
        source_hash: str,
        summary: dict[str, Any],
        snapshot_id: str | None = None,
    ) -> GoalPilotSnapshot:
        ...

    def get_latest_snapshot(self, run_id: str) -> GoalPilotSnapshot | None:
        ...

    def add_action(
        self,
        run_id: str,
        action_type: str,
        input_data: dict[str, Any],
        result_data: dict[str, Any],
        status: str,
        action_id: str | None = None,
    ) -> GoalPilotAction:
        ...

    def list_actions(self, run_id: str) -> list[GoalPilotAction]:
        ...

    def add_note(
        self,
        run_id: str,
        note_type: str,
        note_text: str,
        note_id: str | None = None,
    ) -> GoalPilotNote:
        ...

    def list_notes(self, run_id: str, note_type: str | None = None) -> list[GoalPilotNote]:
        ...

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
        ...

    def list_skills(self, status: str | None = None) -> list[GoalPilotSkill]:
        ...

    def get_skill(self, skill_id: str) -> GoalPilotSkill | None:
        ...
