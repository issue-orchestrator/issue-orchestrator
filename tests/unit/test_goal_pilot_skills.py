from issue_orchestrator.control.goal_pilot_skills import write_skill_manifest
from issue_orchestrator.domain.goal_pilot import GoalPilotSkill


def test_write_skill_manifest(tmp_path):
    skills = [
        GoalPilotSkill(
            skill_id="gpsk-abc123",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            status="active",
            title="UI clarity",
            intent="Improve navigation clarity",
            triggers=["UI feels cluttered"],
            constraints=["Do not change API"],
            playbook="Consolidate navigation into one layout.",
            examples=["Centralized sidebar with consistent labels"],
            sources=["issue-123"],
            last_verified=None,
        )
    ]
    index_path = write_skill_manifest(skills, tmp_path)
    assert index_path.exists()
    skill_file = tmp_path / "gpsk-abc123.yaml"
    assert skill_file.exists()
    content = skill_file.read_text()
    assert "UI clarity" in content
