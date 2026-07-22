"""Goal Pilot control loop skeleton."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, cast

from ..events import EventContext, EventName
from ..ports import EventSink,  make_trace_event
from ..ports.goal_pilot_store import GoalPilotStore
from ..ports.repository_host import RepositoryHost
from .action_applier import ActionApplier
from .actions import AddLabelAction, RemoveLabelAction
from .goal_pilot_skills import write_skill_manifest
from ..infra.repo_identity import state_dir


class GoalPilot:
    """Goal-level controller that can pivot to achieve outcomes.

    This is a thin control-plane façade over the GoalPilotStore. It emits
    events and provides the minimal API needed by future CLI/UI adapters.
    """

    def __init__(
        self,
        store: GoalPilotStore,
        events: EventSink,
        action_applier: ActionApplier,
        repo_root: str | None = None,
        ctx: EventContext | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._action_applier = action_applier
        self._repo_root = repo_root
        self._ctx = ctx or EventContext()

    def create(self, goals: list[str], done_criteria: dict[str, Any], name: str) -> str:
        if not name or not name.strip():
            raise ValueError("GoalPilot run name is required")
        run = self._store.create_run(goals=goals, done_criteria=done_criteria, name=name)
        self._events.publish(
            make_trace_event(
                EventName.GOAL_PILOT_CREATED,
                self._ctx.enrich({"run_id": run.run_id, "goals": goals, "name": name}),
            )
        )
        return run.run_id

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        runs = self._store.list_runs(limit=limit)
        return _serialize_dataclasses(runs)

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        snapshot = self._store.get_latest_snapshot(run_id)
        actions = self._store.list_actions(run_id)
        notes = self._store.list_notes(run_id)
        journeys = self._store.list_journeys(run_id)
        phase_history = self._store.list_phase_history(run_id)
        return {
            "run": _serialize_dataclasses(run),
            "latest_snapshot": _serialize_dataclasses(snapshot),
            "actions": _serialize_dataclasses(actions),
            "notes": _serialize_dataclasses(notes),
            "journeys": _serialize_dataclasses(journeys),
            "phase_history": _serialize_dataclasses(phase_history),
        }

    def update_goals(self, run_id: str, goals: list[str], note: str | None = None) -> dict[str, Any]:
        self._require_run(run_id)
        self._store.update_run_goals(run_id, goals)
        if note:
            self._store.add_note(
                run_id=run_id,
                note_type="goals_update",
                note_text=note,
            )
        self._events.publish(
            make_trace_event(
                EventName.GOAL_PILOT_UPDATED,
                self._ctx.enrich({"run_id": run_id, "goals": goals}),
            )
        )
        return {"run_id": run_id, "goals": goals}

    def set_phase(
        self,
        run_id: str,
        phase: str,
        reason: str,
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self._require_run(run_id)
        changes = changes or {}
        self._store.update_run_phase(run_id, phase)
        self._store.add_phase_change(
            run_id=run_id,
            from_phase=run.phase,
            to_phase=phase,
            reason=reason,
            changes=changes,
        )
        self._events.publish(
            make_trace_event(
                EventName.GOAL_PILOT_UPDATED,
                self._ctx.enrich({"run_id": run_id, "phase": phase, "reason": reason}),
            )
        )
        return {"run_id": run_id, "phase": phase}

    def list_journeys(self, run_id: str) -> list[dict[str, Any]]:
        self._require_run(run_id)
        journeys = self._store.list_journeys(run_id)
        return _serialize_dataclasses(journeys)

    def create_journey(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_run(run_id)
        title = payload.get("title")
        if not title:
            raise ValueError("journey requires title")
        description = payload.get("description", "")
        try:
            order_index = _parse_order_index(payload.get("order_index", 0))
        except ValueError as exc:
            raise ValueError("order_index must be an integer") from exc
        order_index = int(payload.get("order_index", 0))
        priority = payload.get("priority", "medium")
        status = payload.get("status", "planned")
        success_criteria = payload.get("success_criteria", "")
        under_the_covers = payload.get("under_the_covers") or {}
        lookahead = payload.get("lookahead") or {}
        milestone = payload.get("milestone")
        journey = self._store.add_journey(
            run_id=run_id,
            title=title,
            description=description,
            order_index=order_index,
            priority=priority,
            status=status,
            success_criteria=success_criteria,
            under_the_covers=under_the_covers,
            lookahead=lookahead,
            milestone=milestone,
        )
        return _serialize_dataclasses(journey)

    def update_journey(self, journey_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        journey = self._store.update_journey(journey_id, updates)
        return _serialize_dataclasses(journey)

    def reorder_journeys(self, run_id: str, ordered_ids: list[str]) -> dict[str, Any]:
        self._require_run(run_id)
        self._store.reorder_journeys(run_id, ordered_ids)
        return {"run_id": run_id, "order": ordered_ids}

    def next_action(self, run_id: str, record: bool = True) -> dict[str, Any]:
        self._require_run(run_id)
        action = {
            "action_type": "noop",
            "reason": "no_action_available",
        }
        if record:
            self._store.add_action(
                run_id=run_id,
                action_type=action["action_type"],
                input_data=action,
                result_data={},
                status="proposed",
            )
            self._events.publish(
                make_trace_event(
                    EventName.GOAL_PILOT_ACTION_PROPOSED,
                    self._ctx.enrich({"run_id": run_id, "action": action}),
                )
            )
        return action

    @staticmethod
    def supported_actions() -> list[str]:
        """Return the action types Goal Pilot can propose."""
        return [
            "create_issue",
            "create_milestone",
            "reassign_issue_to_milestone",
            "reprioritize",
            "defer_issue",
            "change_approach",
            "dispatch",
            "review",
            "merge",
            "tech_lead",
            "noop",
        ]

    def step(self, run_id: str) -> dict[str, Any]:
        action = self.next_action(run_id, record=False)
        result = {"status": "executed", "action": action}
        self._store.add_action(
            run_id=run_id,
            action_type=action["action_type"],
            input_data=action,
            result_data=result,
            status="executed",
        )
        self._events.publish(
            make_trace_event(
                EventName.GOAL_PILOT_ACTION_EXECUTED,
                self._ctx.enrich({"run_id": run_id, "action": action}),
            )
        )
        return result

    def finish(self, run_id: str) -> dict[str, Any]:
        self._require_run(run_id)
        self._store.update_run_status(run_id, "completed")
        self._events.publish(
            make_trace_event(
                EventName.GOAL_PILOT_COMPLETED,
                self._ctx.enrich({"run_id": run_id}),
            )
        )
        return {"run_id": run_id, "status": "completed"}

    def execute_action(
        self,
        run_id: str,
        action: dict[str, Any],
        repository_host: RepositoryHost,
    ) -> dict[str, Any]:
        """Execute a concrete action against the repository host."""
        self._require_run(run_id)
        action_type = action.get("action_type")
        result: dict[str, Any]
        status = "executed"

        try:
            if action_type == "create_issue":
                result = self._exec_create_issue(action, repository_host)
            elif action_type == "create_milestone":
                result = self._exec_create_milestone(action, repository_host)
            elif action_type == "reassign_issue_to_milestone":
                result = self._exec_reassign_issue(action, repository_host)
            elif action_type in {
                "reprioritize",
                "defer_issue",
                "dispatch",
                "review",
                "merge",
                "tech_lead",
            }:
                result = self._exec_label_update(action, repository_host)
            elif action_type == "change_approach":
                result = self._exec_change_approach(run_id, action)
            elif action_type == "noop":
                result = {"status": "noop"}
            else:
                raise ValueError(f"Unsupported action_type: {action_type}")
        except Exception as exc:
            status = "failed"
            result = {"error": str(exc)}

        self._store.add_action(
            run_id=run_id,
            action_type=action_type or "unknown",
            input_data=action,
            result_data=result,
            status=status,
        )
        if status == "executed":
            event_name = EventName.GOAL_PILOT_ACTION_EXECUTED
        else:
            event_name = EventName.GOAL_PILOT_ACTION_FAILED
        self._events.publish(
            make_trace_event(
                event_name,
                self._ctx.enrich({"run_id": run_id, "action": action, "result": result}),
            )
        )
        return {"status": status, "result": result}

    def _exec_create_issue(self, action: dict[str, Any], repository_host: RepositoryHost) -> dict[str, Any]:
        title = action.get("title")
        body = action.get("body") or ""
        labels = action.get("labels")
        milestone = action.get("milestone")
        if not title:
            raise ValueError("create_issue requires 'title'")
        created = repository_host.create_issue(
            title=title,
            body=body,
            labels=labels,
            milestone=milestone,
        )
        if created is None:
            raise RuntimeError("create_issue failed")
        return {"issue": created}

    def _exec_create_milestone(self, action: dict[str, Any], repository_host: RepositoryHost) -> dict[str, Any]:
        title = action.get("title")
        if not title:
            raise ValueError("create_milestone requires 'title'")
        created = repository_host.create_milestone(
            title=title,
            description=action.get("description"),
            due_on=action.get("due_on"),
            state=action.get("state", "open"),
        )
        if created is None:
            raise RuntimeError("create_milestone failed")
        return {"milestone": created}

    def _exec_reassign_issue(self, action: dict[str, Any], repository_host: RepositoryHost) -> dict[str, Any]:
        issue_number = action.get("issue_number")
        milestone_number = action.get("milestone")
        if issue_number is None:
            raise ValueError("reassign_issue_to_milestone requires 'issue_number'")
        if "milestone_title" in action and milestone_number is None:
            milestone_number = self._resolve_milestone_number(
                action.get("milestone_title"),
                repository_host,
            )
        repository_host.update_issue_milestone(issue_number, milestone_number)
        return {"issue_number": issue_number, "milestone": milestone_number}

    def _exec_label_update(self, action: dict[str, Any], repository_host: RepositoryHost) -> dict[str, Any]:
        issue_number = action.get("issue_number")
        add_labels = _require_label_list(action.get("labels_add"), "labels_add")
        remove_labels = _require_label_list(action.get("labels_remove"), "labels_remove")
        if issue_number is None:
            raise ValueError("label update requires 'issue_number'")
        if not add_labels and not remove_labels:
            raise ValueError("label update requires labels_add or labels_remove")
        for label in add_labels:
            self._action_applier.apply(AddLabelAction(issue_number=issue_number, label=label))
        for label in remove_labels:
            self._action_applier.apply(RemoveLabelAction(issue_number=issue_number, label=label))
        return {"issue_number": issue_number, "labels_add": add_labels, "labels_remove": remove_labels}

    def _exec_change_approach(self, run_id: str, action: dict[str, Any]) -> dict[str, Any]:
        summary = action.get("summary")
        if not summary:
            raise ValueError("change_approach requires 'summary'")
        self._store.add_note(
            run_id=run_id,
            note_type="approach_change",
            note_text=summary,
        )
        return {"summary": summary}

    def _resolve_milestone_number(self, title: str | None, repository_host: RepositoryHost) -> int | None:
        if not title:
            return None
        for milestone in repository_host.list_milestones(state="open"):
            if milestone.get("title") == title:
                return milestone.get("number")
        return None

    def export_skills(self, status: str = "active") -> dict[str, Any]:
        skills = self._store.list_skills(status=status)
        if self._repo_root is None:
            raise ValueError("GoalPilot requires repo_root for skill export")
        output_dir = state_dir(self._repo_root) / "goal_pilot" / "skills"
        index_path = write_skill_manifest(skills, output_dir)
        return {"count": len(skills), "index_path": str(index_path)}

    def list_skills(self, status: str | None = None) -> list[dict[str, Any]]:
        skills = self._store.list_skills(status=status)
        return [skill.__dict__ for skill in skills]

    def upsert_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        skill = self._store.upsert_skill(
            title=payload.get("title", ""),
            intent=payload.get("intent", ""),
            triggers=payload.get("triggers") or [],
            constraints=payload.get("constraints") or [],
            playbook=payload.get("playbook", ""),
            examples=payload.get("examples") or [],
            sources=payload.get("sources") or [],
            status=payload.get("status", "draft"),
            skill_id=payload.get("skill_id"),
            last_verified=payload.get("last_verified"),
        )
        return skill.__dict__

    def _require_run(self, run_id: str) -> Any:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"GoalPilot run not found: {run_id}")
        return run


def _serialize_dataclasses(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(cast(Any, value))
    if isinstance(value, list):
        serialized: list[Any] = []
        for item in value:
            if is_dataclass(item) and not isinstance(item, type):
                serialized.append(asdict(cast(Any, item)))
            else:
                serialized.append(item)
        return serialized
    return value


def _require_label_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return [label for label in value if label]


def _parse_order_index(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("order_index must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return 0
        return int(trimmed)
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError("order_index must be an integer")
    return int(value)
