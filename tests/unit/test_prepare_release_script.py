"""Tests for the local release-prep script."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "prepare_release.py"
SPEC = importlib.util.spec_from_file_location("prepare_release", SCRIPT_PATH)
assert SPEC is not None
prepare_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = prepare_release
SPEC.loader.exec_module(prepare_release)


def _completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _workflow_options(**overrides):
    defaults = {
        "dry_run": True,
        "sync_environment": True,
        "assume_yes": False,
        "skip_validation": False,
        "validation_command": ("make", "validate-pr"),
        "push": True,
        "create_github_release": True,
        "uv_executable": None,
    }
    defaults.update(overrides)
    return prepare_release.ReleaseWorkflowOptions(**defaults)


def _write_pyproject(path: Path, *, version: str = "0.9.0") -> None:
    path.write_text(
        f"""[project]
name = "issue-orchestrator"
version = "{version}"

[tool.example]
version = "9.9.9"
""",
        encoding="utf-8",
    )


def _write_lock(path: Path, *, version: str = "0.9.0") -> None:
    path.write_text(
        f"""version = 1

[[package]]
name = "issue-orchestrator"
version = "{version}"
source = {{ editable = "." }}

[[package]]
name = "other-package"
version = "9.9.9"
source = {{ registry = "https://pypi.org/simple" }}
""",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("raw_version", "expected"),
    [
        ("1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3"),
        ("V1.2.3", "1.2.3"),
    ],
)
def test_normalize_release_version_accepts_stable_semver(
    raw_version: str, expected: str
) -> None:
    assert prepare_release.normalize_release_version(raw_version) == expected


@pytest.mark.parametrize("raw_version", ["1.2", "1.2.3rc1", "01.2.3", "latest"])
def test_normalize_release_version_rejects_non_release_versions(
    raw_version: str,
) -> None:
    with pytest.raises(prepare_release.ReleasePrepError):
        prepare_release.normalize_release_version(raw_version)


def test_write_project_version_updates_only_project_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)

    previous = prepare_release.write_project_version(pyproject, "1.0.0")

    assert previous == "0.9.0"
    assert prepare_release.read_project_version(pyproject) == "1.0.0"
    content = pyproject.read_text(encoding="utf-8")
    assert '[tool.example]\nversion = "9.9.9"' in content


def test_read_lock_project_version_uses_editable_project_entry(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    _write_lock(lockfile, version="1.2.3")

    assert prepare_release.read_lock_project_version(lockfile) == "1.2.3"


def test_verify_project_and_lock_versions_requires_matching_release(
    tmp_path: Path,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="1.2.3")
    _write_lock(tmp_path / "uv.lock", version="1.2.2")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    with pytest.raises(prepare_release.ReleasePrepError, match="uv.lock"):
        prepare_release.verify_project_and_lock_versions(paths, "1.2.3")


def test_confirm_release_requires_exact_tag() -> None:
    with pytest.raises(prepare_release.ReleasePrepError, match="expected exact"):
        prepare_release.confirm_release(
            tag_name="v1.0.0",
            input_func=lambda _prompt: "1.0.0",
        )


def test_parse_command_rejects_empty_command() -> None:
    with pytest.raises(prepare_release.ReleasePrepError, match="empty"):
        prepare_release.parse_command("")


def test_full_release_dry_run_prints_single_workflow_without_mutating_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml")
    _write_lock(tmp_path / "uv.lock")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    preflight_calls: list[str] = []

    def fake_dry_run_preflight(*, paths, tag_name, options):  # noqa: ANN001
        preflight_calls.append(tag_name)

    monkeypatch.setattr(
        prepare_release,
        "run_release_dry_run_preflight",
        fake_dry_run_preflight,
    )

    prepare_release.run_release_workflow(
        paths=paths,
        version="v0.9.0",
        options=_workflow_options(),
    )

    output = capsys.readouterr().out
    assert "Preparing full issue-orchestrator release v0.9.0" in output
    assert "Version 0.9.0 already present in pyproject.toml and uv.lock." in output
    assert "Dry run: no files, git refs, or GitHub releases will be changed." in output
    assert "+ git status --porcelain" in output
    assert "+ git ls-remote --exit-code origin refs/heads/main" in output
    assert "+ make validate-pr" in output
    assert "+ git push origin refs/tags/v0.9.0:refs/tags/v0.9.0" in output
    assert "+ gh release create v0.9.0 --generate-notes" in output
    assert "git commit" not in output
    assert "HEAD:main" not in output
    assert preflight_calls == ["v0.9.0"]
    assert prepare_release.read_project_version(tmp_path / "pyproject.toml") == "0.9.0"
    assert prepare_release.read_lock_project_version(tmp_path / "uv.lock") == "0.9.0"


def test_full_release_requires_version_already_merged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="0.9.0")
    _write_lock(tmp_path / "uv.lock", version="0.9.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    monkeypatch.setattr(
        prepare_release,
        "run_release_dry_run_preflight",
        lambda *, paths, tag_name, options: None,
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="prepare-release"):
        prepare_release.run_release_workflow(
            paths=paths,
            version="v1.0.0",
            options=_workflow_options(),
        )


def test_full_release_rejects_github_release_without_push(tmp_path: Path) -> None:
    _write_pyproject(tmp_path / "pyproject.toml")
    _write_lock(tmp_path / "uv.lock")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)

    with pytest.raises(prepare_release.ReleasePrepError, match="local-only"):
        prepare_release.run_release_workflow(
            paths=paths,
            version="v1.0.0",
            options=_workflow_options(push=False, create_github_release=True),
        )


def test_assert_current_branch_is_main_rejects_other_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_release,
        "run_captured_command",
        lambda _command, *, cwd: _completed(stdout="feature\n"),
    )

    with pytest.raises(prepare_release.ReleasePrepError, match="local branch 'main'"):
        prepare_release.assert_current_branch_is_main(tmp_path)


def test_assert_head_matches_origin_main_rejects_outdated_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_rev_parse(_root: Path, ref: str) -> str:
        return {
            "HEAD": "aaa",
            "refs/remotes/origin/main": "bbb",
        }[ref]

    monkeypatch.setattr(prepare_release, "git_rev_parse", fake_rev_parse)

    with pytest.raises(prepare_release.ReleasePrepError, match="origin/main"):
        prepare_release.assert_head_matches_origin_main(tmp_path)


def test_assert_head_matches_remote_main_rejects_outdated_remote_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prepare_release, "git_rev_parse", lambda _root, _ref: "aaa")
    monkeypatch.setattr(prepare_release, "remote_main_sha", lambda _root: "bbb")

    with pytest.raises(
        prepare_release.ReleasePrepError, match="current remote origin/main"
    ):
        prepare_release.assert_head_matches_remote_main(tmp_path)


def test_apply_release_metadata_resolves_uv_before_mutating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    events: list[str] = []

    def fake_find_uv(_explicit_uv):  # noqa: ANN001
        events.append("find_uv")
        return "uv"

    def fake_write_project_version(_pyproject, _version):  # noqa: ANN001
        events.append("write_project_version")
        return "0.9.0"

    monkeypatch.setattr(prepare_release, "find_uv", fake_find_uv)
    monkeypatch.setattr(prepare_release, "write_project_version", fake_write_project_version)
    monkeypatch.setattr(
        prepare_release,
        "run_command",
        lambda _command, *, cwd: events.append("run_command"),
    )
    monkeypatch.setattr(
        prepare_release,
        "verify_project_and_lock_versions",
        lambda _paths, _version: events.append("verify_versions"),
    )
    monkeypatch.setattr(
        prepare_release,
        "sync_environment_if_requested",
        lambda *, paths, expected_version, sync_environment, uv_executable: events.append(
            "sync_environment"
        ),
    )

    prepare_release.apply_release_metadata(
        paths=paths,
        target_version="1.0.0",
        sync_environment=True,
        uv_executable=None,
    )

    assert events[:2] == ["find_uv", "write_project_version"]


def test_full_release_rechecks_clean_tree_after_validation_before_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path / "pyproject.toml", version="1.0.0")
    _write_lock(tmp_path / "uv.lock", version="1.0.0")
    paths = prepare_release.ReleasePaths.from_root(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        prepare_release,
        "run_release_preflight",
        lambda *, paths, tag_name, options: calls.append("preflight"),
    )
    monkeypatch.setattr(
        prepare_release,
        "verify_release_metadata_ready",
        lambda _paths, _version: calls.append("verify_metadata"),
    )
    monkeypatch.setattr(
        prepare_release,
        "confirm_release",
        lambda *, tag_name: calls.append("confirm"),
    )
    monkeypatch.setattr(
        prepare_release,
        "sync_environment_if_requested",
        lambda *, paths, expected_version, sync_environment, uv_executable: calls.append(
            "sync"
        ),
    )
    monkeypatch.setattr(
        prepare_release,
        "run_release_validation",
        lambda _paths, _options: calls.append("validate"),
    )
    monkeypatch.setattr(
        prepare_release,
        "assert_clean_worktree",
        lambda _root: calls.append("clean_after_validation"),
    )
    monkeypatch.setattr(
        prepare_release,
        "create_release_tag",
        lambda _paths, _tag_name: calls.append("tag"),
    )
    monkeypatch.setattr(
        prepare_release,
        "publish_release",
        lambda _paths, _tag_name, _options: calls.append("publish"),
    )

    prepare_release.run_release_workflow(
        paths=paths,
        version="v1.0.0",
        options=_workflow_options(dry_run=False),
    )

    assert calls == [
        "preflight",
        "verify_metadata",
        "confirm",
        "sync",
        "validate",
        "clean_after_validation",
        "tag",
        "publish",
    ]
