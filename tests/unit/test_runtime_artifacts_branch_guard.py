"""Tests for the forbidden-on-branch runtime-artifact guard (#6659).

This guard is distinct from the dirty-tree filters: it answers "is this path,
present in a *committed* branch diff against base, a runtime artifact that must
never have entered the branch?". Runtime outputs under ``.issue-orchestrator/``
(review-exchange prompts, persistent-pair recordings, validation records, …)
break the reviewer-worktree fast-forward checkout when committed, so they must
be rejected before publish/review-exchange finalization. Project-owned config is
allowlisted and must pass.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.infra.runtime_artifacts import (
    branch_post_image_paths_from_diff,
    build_forbidden_runtime_artifact_reason,
    forbidden_branch_runtime_artifacts,
    is_forbidden_branch_runtime_artifact,
)


# ---------------------------------------------------------------------------
# is_forbidden_branch_runtime_artifact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".issue-orchestrator/persistent-pairs/issue-6594/coder/terminal-recording.jsonl",
        ".issue-orchestrator/persistent-pairs/issue-6594/validation-record.json",
        ".issue-orchestrator/review-feedback/cycle-1.md",
        ".issue-orchestrator/review-response.json",
        ".issue-orchestrator/review-report.md",
        ".issue-orchestrator/review-exchange-turn-prompt.md",
        ".issue-orchestrator/tool-homes/codex/config.toml",
        # A runtime output we have not enumerated explicitly is still caught
        # because the guard is allowlist-based, not denylist-based.
        ".issue-orchestrator/some-future-runtime-output.json",
    ],
)
def test_runtime_outputs_are_forbidden(path: str) -> None:
    assert is_forbidden_branch_runtime_artifact(path) is True


@pytest.mark.parametrize(
    "path",
    [
        ".issue-orchestrator/config/main.yaml",
        ".issue-orchestrator/config/hooks-validate.yaml",
        ".issue-orchestrator/runtime-ignore",
        ".issue-orchestrator/allow-no-verify-dry-run",
    ],
)
def test_tracked_project_files_are_allowed(path: str) -> None:
    assert is_forbidden_branch_runtime_artifact(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/issue_orchestrator/control/completion_processor.py",
        "README.md",
        # ``.claude/`` is legitimately tracked (hooks, settings, skills) so it
        # is intentionally outside the branch guard's roots.
        ".claude/settings.json",
        ".claude/hooks/pre_push.py",
    ],
)
def test_non_guarded_paths_are_allowed(path: str) -> None:
    assert is_forbidden_branch_runtime_artifact(path) is False


def test_leading_slash_and_backslash_are_normalized() -> None:
    assert is_forbidden_branch_runtime_artifact(
        "/.issue-orchestrator/review-response.json"
    )
    assert is_forbidden_branch_runtime_artifact(
        ".issue-orchestrator\\review-feedback\\cycle-1.md"
    )


# ---------------------------------------------------------------------------
# forbidden_branch_runtime_artifacts
# ---------------------------------------------------------------------------


def test_forbidden_filter_dedupes_and_sorts() -> None:
    paths = [
        ".issue-orchestrator/review-response.json",
        ".issue-orchestrator/config/main.yaml",
        ".issue-orchestrator/persistent-pairs/issue-1/coder/rec.jsonl",
        ".issue-orchestrator/review-response.json",
        "src/main.py",
    ]
    assert forbidden_branch_runtime_artifacts(paths) == [
        ".issue-orchestrator/persistent-pairs/issue-1/coder/rec.jsonl",
        ".issue-orchestrator/review-response.json",
    ]


def test_forbidden_filter_empty_when_only_allowlisted() -> None:
    assert forbidden_branch_runtime_artifacts(
        [".issue-orchestrator/config/main.yaml", "src/app.py"]
    ) == []


# ---------------------------------------------------------------------------
# branch_post_image_paths_from_diff
# ---------------------------------------------------------------------------


def test_diff_parser_extracts_added_and_modified_paths() -> None:
    diff = (
        "diff --git a/.issue-orchestrator/review-response.json "
        "b/.issue-orchestrator/review-response.json\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/.issue-orchestrator/review-response.json\n"
        "@@ -0,0 +1 @@\n"
        "+{}\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "index 2222222..3333333 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert branch_post_image_paths_from_diff(diff) == [
        ".issue-orchestrator/review-response.json",
        "src/app.py",
    ]


def test_diff_parser_skips_deletions() -> None:
    # A branch that *removes* a previously-committed artifact is fine; the
    # post-image is /dev/null, so the path must not be reported.
    diff = (
        "diff --git a/.issue-orchestrator/review-response.json "
        "b/.issue-orchestrator/review-response.json\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/.issue-orchestrator/review-response.json\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-{}\n"
    )
    assert branch_post_image_paths_from_diff(diff) == []


def test_diff_parser_handles_empty_diff() -> None:
    assert branch_post_image_paths_from_diff("") == []


def test_diff_parser_extracts_binary_addition() -> None:
    # Binary files carry no ``+++ b/...`` line; the post-image path is only on
    # the ``Binary files /dev/null and b/<path> differ`` line. A committed
    # binary runtime artifact (e.g. a tool-home blob) must still be detected.
    diff = (
        "diff --git a/.issue-orchestrator/tool-homes/blob.bin "
        "b/.issue-orchestrator/tool-homes/blob.bin\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "Binary files /dev/null and b/.issue-orchestrator/tool-homes/blob.bin differ\n"
    )
    assert branch_post_image_paths_from_diff(diff) == [
        ".issue-orchestrator/tool-homes/blob.bin",
    ]


def test_diff_parser_extracts_binary_modification() -> None:
    diff = (
        "diff --git a/.issue-orchestrator/tool-homes/blob.bin "
        "b/.issue-orchestrator/tool-homes/blob.bin\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/.issue-orchestrator/tool-homes/blob.bin "
        "and b/.issue-orchestrator/tool-homes/blob.bin differ\n"
    )
    assert branch_post_image_paths_from_diff(diff) == [
        ".issue-orchestrator/tool-homes/blob.bin",
    ]


def test_diff_parser_skips_binary_deletion() -> None:
    # Removing a previously-committed binary artifact is fine: the post-image
    # is /dev/null, so nothing should be reported.
    diff = (
        "diff --git a/.issue-orchestrator/tool-homes/blob.bin "
        "b/.issue-orchestrator/tool-homes/blob.bin\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "Binary files a/.issue-orchestrator/tool-homes/blob.bin and /dev/null differ\n"
    )
    assert branch_post_image_paths_from_diff(diff) == []


def test_binary_artifact_is_caught_end_to_end() -> None:
    # The guard must block a committed binary runtime artifact, mirroring the
    # text-file path: parse the diff, then classify the post-image paths.
    diff = (
        "diff --git a/.issue-orchestrator/tool-homes/blob.bin "
        "b/.issue-orchestrator/tool-homes/blob.bin\n"
        "new file mode 100644\n"
        "Binary files /dev/null and b/.issue-orchestrator/tool-homes/blob.bin differ\n"
    )
    assert forbidden_branch_runtime_artifacts(
        branch_post_image_paths_from_diff(diff)
    ) == [".issue-orchestrator/tool-homes/blob.bin"]


def test_parser_and_filter_compose_on_a_real_world_branch() -> None:
    # The #6594 incident: rework branch carried runtime artifacts.
    diff = "".join(
        "diff --git a/{p} b/{p}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/{p}\n"
        "@@ -0,0 +1 @@\n"
        "+x\n".format(p=p)
        for p in [
            ".issue-orchestrator/persistent-pairs/issue-6594/coder/terminal-recording.jsonl",
            ".issue-orchestrator/review-exchange-turn-prompt.md",
            ".issue-orchestrator/review-feedback/cycle-1.md",
            ".issue-orchestrator/config/main.yaml",
            "src/issue_orchestrator/control/foo.py",
        ]
    )
    forbidden = forbidden_branch_runtime_artifacts(
        branch_post_image_paths_from_diff(diff)
    )
    assert forbidden == [
        ".issue-orchestrator/persistent-pairs/issue-6594/coder/terminal-recording.jsonl",
        ".issue-orchestrator/review-exchange-turn-prompt.md",
        ".issue-orchestrator/review-feedback/cycle-1.md",
    ]


# ---------------------------------------------------------------------------
# build_forbidden_runtime_artifact_reason
# ---------------------------------------------------------------------------


def test_reason_lists_paths_and_is_actionable() -> None:
    reason = build_forbidden_runtime_artifact_reason(
        [".issue-orchestrator/review-response.json"]
    )
    assert "runtime artifacts" in reason
    assert "git rm --cached" in reason
    assert ".issue-orchestrator/review-response.json" in reason


def test_reason_truncates_long_lists() -> None:
    paths = [f".issue-orchestrator/persistent-pairs/issue-{i}/rec.jsonl" for i in range(12)]
    reason = build_forbidden_runtime_artifact_reason(paths)
    assert "(+4 more)" in reason
