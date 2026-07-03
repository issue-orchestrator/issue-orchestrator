from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GROUP_AWARE_PATHS_FILTER = (
    "dorny/paths-filter@7b450fff21473bca461d4b92ce414b9d0420d706"
)


def test_validate_workflow_runs_required_checks_for_merge_queue() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/validate.yml").read_text())

    triggers = workflow[True]  # PyYAML treats the YAML 1.1 "on" key as True.
    assert "merge_group" in triggers

    jobs = workflow["jobs"]
    changes_steps = jobs["changes"]["steps"]
    path_filter_step = next(
        step for step in changes_steps if step.get("name") == "Classify changed paths"
    )
    assert path_filter_step["uses"] == MERGE_GROUP_AWARE_PATHS_FILTER

    for job_name in ("validate-fast", "validate-agent"):
        assert jobs[job_name]["if"] == (
            "github.event_name == 'merge_group' || "
            "needs.changes.outputs.python == 'true'"
        )
