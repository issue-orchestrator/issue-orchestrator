"""Goal Pilot skill manifest generation."""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.goal_pilot import GoalPilotSkill


def _yaml_list(items: list[str], indent: int = 2) -> list[str]:
    prefix = " " * indent + "- "
    return [f"{prefix}{item}" for item in items]


def render_skill_yaml(skill: GoalPilotSkill) -> str:
    lines = [
        "---",
        f"id: {skill.skill_id}",
        f"title: {skill.title}",
        f"status: {skill.status}",
        f"intent: {json.dumps(skill.intent)}",
        "triggers:",
        *_yaml_list(skill.triggers),
        "constraints:",
        *_yaml_list(skill.constraints),
        f"playbook: {json.dumps(skill.playbook)}",
        "examples:",
        *_yaml_list(skill.examples),
        "sources:",
        *_yaml_list(skill.sources),
        f"last_verified: {skill.last_verified or ''}",
        "---",
        "",
    ]
    return "\n".join(lines)


def write_skill_manifest(skills: list[GoalPilotSkill], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_lines = [
        "# Goal Pilot Skills Index",
        "",
        "This index is generated from the Goal Pilot skill store.",
        "",
    ]
    for skill in skills:
        if skill.status == "deprecated":
            continue
        filename = f"{skill.skill_id}.yaml"
        (output_dir / filename).write_text(render_skill_yaml(skill))
        index_lines.append(f"- {skill.title} ({skill.status}) -> {filename}")
    index_path = output_dir / "index.md"
    index_path.write_text("\n".join(index_lines).strip() + "\n")
    return index_path
