"""Domain types for Goal Pilot runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoalPilotRun:
    run_id: str
    created_at: str
    updated_at: str
    status: str
    name: str
    phase: str
    goals: list[str]
    done_criteria: dict[str, Any]


@dataclass(frozen=True)
class GoalPilotSnapshot:
    snapshot_id: str
    run_id: str
    created_at: str
    source_hash: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class GoalPilotAction:
    action_id: str
    run_id: str
    created_at: str
    action_type: str
    input_data: dict[str, Any]
    result_data: dict[str, Any]
    status: str


@dataclass(frozen=True)
class GoalPilotNote:
    note_id: str
    run_id: str
    created_at: str
    note_type: str
    note_text: str


@dataclass(frozen=True)
class GoalPilotSkill:
    skill_id: str
    created_at: str
    updated_at: str
    status: str
    title: str
    intent: str
    triggers: list[str]
    constraints: list[str]
    playbook: str
    examples: list[str]
    sources: list[str]
    last_verified: str | None


@dataclass(frozen=True)
class GoalPilotPhaseChange:
    phase_id: str
    run_id: str
    created_at: str
    from_phase: str
    to_phase: str
    reason: str
    changes: dict[str, Any]


@dataclass(frozen=True)
class GoalPilotJourney:
    journey_id: str
    run_id: str
    created_at: str
    updated_at: str
    title: str
    description: str
    order_index: int
    priority: str
    status: str
    success_criteria: str
    under_the_covers: dict[str, Any]
    lookahead: dict[str, Any]
    milestone: str | None
