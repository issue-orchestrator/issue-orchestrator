import pytest

from issue_orchestrator.execution.goal_pilot_store import SqliteGoalPilotStore


def test_goal_pilot_store_round_trip(tmp_path):
    db_path = tmp_path / "goal_pilot.sqlite"
    store = SqliteGoalPilotStore(repo_root=tmp_path, db_path=db_path)

    run = store.create_run(
        goals=["ui clarity", "nav ease"],
        done_criteria={"all_closed": True},
        name="UI clarity sprint",
    )
    fetched = store.get_run(run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id
    assert fetched.name == "UI clarity sprint"
    assert fetched.phase == "outcomes_opportunities"
    assert fetched.goals == ["ui clarity", "nav ease"]
    assert fetched.done_criteria == {"all_closed": True}

    store.update_run_status(run.run_id, "blocked")
    updated = store.get_run(run.run_id)
    assert updated is not None
    assert updated.status == "blocked"

    snapshot = store.add_snapshot(
        run_id=run.run_id,
        source_hash="hash-1",
        summary={"open": 3},
    )
    latest = store.get_latest_snapshot(run.run_id)
    assert latest is not None
    assert latest.snapshot_id == snapshot.snapshot_id
    assert latest.summary == {"open": 3}

    action = store.add_action(
        run_id=run.run_id,
        action_type="dispatch",
        input_data={"max_sessions": 2},
        result_data={"started": [101, 102]},
        status="executed",
    )
    actions = store.list_actions(run.run_id)
    assert [a.action_id for a in actions] == [action.action_id]
    assert actions[0].status == "executed"

    note = store.add_note(
        run_id=run.run_id,
        note_type="summary",
        note_text="Kickoff",
    )
    notes = store.list_notes(run.run_id)
    assert [n.note_id for n in notes] == [note.note_id]
    assert notes[0].note_text == "Kickoff"

    phase_change = store.add_phase_change(
        run_id=run.run_id,
        from_phase="outcomes_opportunities",
        to_phase="critical_journeys",
        reason="Journey mapping started",
        changes={"journeys": 2},
    )
    history = store.list_phase_history(run.run_id)
    assert history[0].phase_id == phase_change.phase_id

    journey = store.add_journey(
        run_id=run.run_id,
        title="Browse issues",
        description="Navigate quickly",
        order_index=0,
        priority="high",
        status="planned",
        success_criteria="Fast comprehension",
        under_the_covers={"architecture": True},
        lookahead={"status": "green"},
        milestone="M1",
    )
    journeys = store.list_journeys(run.run_id)
    assert [j.journey_id for j in journeys] == [journey.journey_id]

    journey_two = store.add_journey(
        run_id=run.run_id,
        title="Review status",
        description="Understand progress",
        order_index=1,
        priority="medium",
        status="planned",
        success_criteria="Clear state",
        under_the_covers={},
        lookahead={},
        milestone=None,
    )
    with pytest.raises(ValueError, match="must include all journeys"):
        store.reorder_journeys(run.run_id, [journey_two.journey_id])

    skill = store.upsert_skill(
        title="UI clarity",
        intent="Improve navigation clarity",
        triggers=["UI feels cluttered"],
        constraints=["Do not change API"],
        playbook="Consolidate navigation into one layout.",
        examples=["Centralized sidebar with consistent labels"],
        sources=["issue-123"],
        status="active",
    )
    fetched_skill = store.get_skill(skill.skill_id)
    assert fetched_skill is not None
    assert fetched_skill.title == "UI clarity"
    assert fetched_skill.status == "active"
    active_skills = store.list_skills(status="active")
    assert [s.skill_id for s in active_skills] == [skill.skill_id]
