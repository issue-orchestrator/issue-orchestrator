import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GROUP_AWARE_PATHS_FILTER = (
    "dorny/paths-filter@7b450fff21473bca461d4b92ce414b9d0420d706"
)
VSCODE_PACKAGE_JSON = REPO_ROOT / "packages/vscode/package.json"


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


def test_validate_vscode_is_merge_group_aware() -> None:
    """validate-vscode must run in the merge queue and on node-path changes.

    It gates on the `node` filter output (not `python`), so a merge-group run or
    any packages/vscode change compiles the extension. Without the merge_group
    arm, a required check could pass at PR time but never re-run against the
    real merge result.
    """
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/validate.yml").read_text())
    jobs = workflow["jobs"]

    assert "node" in jobs["changes"]["outputs"]
    assert jobs["validate-vscode"]["if"] == (
        "github.event_name == 'merge_group' || "
        "needs.changes.outputs.node == 'true'"
    )


def test_vscode_types_pinned_to_engines_floor() -> None:
    """@types/vscode must not drift above the declared engines.vscode floor.

    A caret range on @types/vscode lets the compile-time API surface advance past
    the minimum VS Code the extension claims to support, so `tsc` would accept
    APIs absent from the oldest supported client. Pin the types to the same major
    the engines floor declares; bumping one requires deliberately bumping both.
    """
    pkg = json.loads(VSCODE_PACKAGE_JSON.read_text())
    engines_floor = pkg["engines"]["vscode"].lstrip("^~")
    types_range = pkg["devDependencies"]["@types/vscode"]

    # Types must be pinned (~ or exact), not caret, and match the engines major.minor.
    assert not types_range.startswith("^"), (
        f"@types/vscode is caret-ranged ({types_range}); pin it to ~ the engines "
        f"floor ({engines_floor}) so it cannot exceed the declared minimum API."
    )
    types_floor = types_range.lstrip("~")
    engines_major_minor = ".".join(engines_floor.split(".")[:2])
    types_major_minor = ".".join(types_floor.split(".")[:2])
    assert types_major_minor == engines_major_minor, (
        f"@types/vscode ({types_range}) must match engines.vscode floor "
        f"({engines_floor}); update both together when raising the floor."
    )
