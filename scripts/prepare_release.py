#!/usr/bin/env python3
"""Prepare a local issue-orchestrator release version bump."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


PROJECT_NAME = "issue-orchestrator"
DEFAULT_VALIDATION_COMMAND = ("make", "validate-pr")
RELEASE_BRANCH = "main"
RELEASE_REMOTE = "origin"
_STABLE_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SECTION_HEADER_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_VERSION_ASSIGNMENT_RE = re.compile(r'^(\s*version\s*=\s*)"([^"]*)"(\s*(?:#.*)?)$')


class ReleasePrepError(RuntimeError):
    """Raised when the release cannot be prepared safely."""


@dataclass(frozen=True)
class ReleasePaths:
    """Filesystem paths used by the release-prep workflow."""

    root: Path
    pyproject: Path
    lockfile: Path
    venv_python: Path

    @classmethod
    def from_root(cls, root: Path) -> "ReleasePaths":
        root = root.resolve()
        return cls(
            root=root,
            pyproject=root / "pyproject.toml",
            lockfile=root / "uv.lock",
            venv_python=root / ".venv" / "bin" / "python",
        )


@dataclass(frozen=True)
class ReleaseWorkflowOptions:
    """Options for the full release workflow."""

    dry_run: bool
    sync_environment: bool
    assume_yes: bool
    skip_validation: bool
    validation_command: tuple[str, ...]
    push: bool
    create_github_release: bool
    uv_executable: str | None


def normalize_release_version(raw_version: str) -> str:
    """Return a stable SemVer release version without a leading ``v``."""
    version = raw_version.strip()
    if version[:1].lower() == "v":
        version = version[1:]
    if not _STABLE_SEMVER_RE.fullmatch(version):
        raise ReleasePrepError(
            "Release version must be stable SemVer like 1.2.3 or v1.2.3; "
            f"got {raw_version!r}"
        )
    return version


def read_project_version(pyproject_path: Path) -> str:
    """Read the PEP 621 project version from ``pyproject.toml``."""
    with pyproject_path.open("rb") as file:
        data = tomllib.load(file)
    project = data.get("project")
    if not isinstance(project, dict):
        raise ReleasePrepError(f"{pyproject_path} has no [project] table")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ReleasePrepError(f"{pyproject_path} has no [project].version string")
    return version


def write_project_version(pyproject_path: Path, new_version: str) -> str:
    """Update only ``[project].version`` and return the previous version."""
    previous_version = read_project_version(pyproject_path)
    lines = pyproject_path.read_text(encoding="utf-8").splitlines(keepends=True)

    in_project_section = False
    version_line_indexes: list[int] = []
    for index, line in enumerate(lines):
        section_match = _SECTION_HEADER_RE.match(line.rstrip("\r\n"))
        if section_match:
            section_name = section_match.group(1)
            if section_name == "project":
                in_project_section = True
                continue
            if in_project_section:
                break

        if in_project_section and _VERSION_ASSIGNMENT_RE.match(line.rstrip("\r\n")):
            version_line_indexes.append(index)

    if not version_line_indexes:
        raise ReleasePrepError(
            f"{pyproject_path} has no version assignment in [project]"
        )
    if len(version_line_indexes) > 1:
        raise ReleasePrepError(
            f"{pyproject_path} has multiple version assignments in [project]"
        )

    line_index = version_line_indexes[0]
    original_line = lines[line_index]
    newline = "\n" if original_line.endswith("\n") else ""
    body = original_line[:-1] if newline else original_line
    if body.endswith("\r"):
        body = body[:-1]
        newline = "\r\n"

    assignment_match = _VERSION_ASSIGNMENT_RE.match(body)
    if assignment_match is None:
        raise ReleasePrepError(f"Could not parse version line in {pyproject_path}")

    prefix, _old, suffix = assignment_match.groups()
    lines[line_index] = f'{prefix}"{new_version}"{suffix}{newline}'
    pyproject_path.write_text("".join(lines), encoding="utf-8")
    return previous_version


def read_lock_project_version(lock_path: Path) -> str:
    """Read the editable project version recorded in ``uv.lock``."""
    with lock_path.open("rb") as file:
        data = tomllib.load(file)
    packages = data.get("package")
    if not isinstance(packages, list):
        raise ReleasePrepError(f"{lock_path} has no package entries")

    for package in packages:
        if not isinstance(package, dict):
            continue
        if package.get("name") != PROJECT_NAME:
            continue
        source = package.get("source")
        if isinstance(source, dict) and source.get("editable") == ".":
            version = package.get("version")
            if isinstance(version, str) and version:
                return version
            raise ReleasePrepError(
                f"{lock_path} has an editable {PROJECT_NAME} without a version"
            )

    raise ReleasePrepError(f"{lock_path} has no editable {PROJECT_NAME} package entry")


def verify_project_and_lock_versions(
    paths: ReleasePaths, expected_version: str
) -> None:
    """Fail if pyproject and uv.lock do not agree on the release version."""
    project_version = read_project_version(paths.pyproject)
    if project_version != expected_version:
        raise ReleasePrepError(
            f"{paths.pyproject} version is {project_version}, expected {expected_version}"
        )

    lock_version = read_lock_project_version(paths.lockfile)
    if lock_version != expected_version:
        raise ReleasePrepError(
            f"{paths.lockfile} version is {lock_version}, expected {expected_version}; "
            "run uv lock"
        )


def find_uv(explicit_uv: str | None = None) -> str:
    """Return the uv executable path, honoring an explicit value or ``UV``."""
    candidates = [explicit_uv, os.environ.get("UV"), shutil.which("uv")]
    for candidate in candidates:
        if candidate:
            return candidate
    raise ReleasePrepError(
        "uv is required to refresh uv.lock; install uv or set UV=/path/to/uv"
    )


def run_command(command: Sequence[str], *, cwd: Path) -> None:
    """Run a command and fail loudly on non-zero exit."""
    printable = " ".join(shlex.quote(part) for part in command)
    print(f"+ {printable}")
    result = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603
    if result.returncode != 0:
        raise ReleasePrepError(
            f"Command failed with exit code {result.returncode}: {printable}"
        )


def run_optional_command(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run a command and return captured output without raising."""
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def run_captured_command(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run a command, capture output, and fail loudly on non-zero exit."""
    result = run_optional_command(command, cwd=cwd)
    if result.returncode != 0:
        printable = " ".join(shlex.quote(part) for part in command)
        details = "\n".join(
            part
            for part in (
                result.stdout.strip(),
                result.stderr.strip(),
            )
            if part
        )
        raise ReleasePrepError(
            f"Command failed with exit code {result.returncode}: {printable}\n{details}"
        )
    return result


def read_installed_package_version(python_executable: Path, *, cwd: Path) -> str:
    """Return installed package metadata from a Python environment."""
    command = [
        str(python_executable),
        "-c",
        (f"from importlib.metadata import version; print(version({PROJECT_NAME!r}))"),
    ]
    result = run_optional_command(command, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not read installed {PROJECT_NAME} metadata from {python_executable}: {detail}"
        )
    return result.stdout.strip()


def verify_installed_package_version(
    paths: ReleasePaths, expected_version: str
) -> None:
    """Fail if the local venv metadata would render a stale Control Center version."""
    if not paths.venv_python.exists():
        print("No .venv Python found; skipping installed metadata verification.")
        return

    installed_version = read_installed_package_version(
        paths.venv_python, cwd=paths.root
    )
    if installed_version != expected_version:
        raise ReleasePrepError(
            f"Installed package metadata is {installed_version}, expected {expected_version}. "
            "Run uv sync --frozen --all-extras before opening the Control Center."
        )


def parse_command(raw_command: str) -> tuple[str, ...]:
    """Parse a configured shell-like command into argv."""
    command = tuple(shlex.split(raw_command))
    if not command:
        raise ReleasePrepError("Command cannot be empty")
    return command


def assert_tool_available(executable: str) -> None:
    """Fail if a required executable is not on PATH."""
    if shutil.which(executable) is None:
        raise ReleasePrepError(f"Required executable not found on PATH: {executable}")


def assert_clean_worktree(root: Path) -> None:
    """Require a clean git worktree before the script owns release changes."""
    result = run_captured_command(["git", "status", "--porcelain"], cwd=root)
    status = result.stdout.strip()
    if status:
        raise ReleasePrepError(
            "Release requires a clean git worktree before it starts.\n"
            "Commit, stash, or remove these changes first:\n"
            f"{status}"
        )


def assert_origin_remote_exists(root: Path) -> None:
    """Fail if the release remote is not configured."""
    result = run_captured_command(
        ["git", "remote", "get-url", RELEASE_REMOTE], cwd=root
    )
    remote_url = result.stdout.strip()
    if not remote_url:
        raise ReleasePrepError(f"Git remote {RELEASE_REMOTE!r} has no URL")


def fetch_origin_main(root: Path) -> None:
    """Fetch the release branch and tags before comparing local state."""
    run_command(
        [
            "git",
            "fetch",
            "--tags",
            RELEASE_REMOTE,
            f"refs/heads/{RELEASE_BRANCH}:refs/remotes/{RELEASE_REMOTE}/{RELEASE_BRANCH}",
        ],
        cwd=root,
    )


def assert_current_branch_is_main(root: Path) -> None:
    """Require releases to be run from the local main branch."""
    result = run_captured_command(["git", "branch", "--show-current"], cwd=root)
    current_branch = result.stdout.strip()
    if current_branch != RELEASE_BRANCH:
        raise ReleasePrepError(
            f"Release must run from local branch {RELEASE_BRANCH!r}; "
            f"current branch is {current_branch or '<detached HEAD>'!r}"
        )


def git_rev_parse(root: Path, ref: str) -> str:
    """Resolve a git ref to a commit SHA."""
    result = run_captured_command(["git", "rev-parse", "--verify", ref], cwd=root)
    return result.stdout.strip()


def assert_head_matches_origin_main(root: Path) -> None:
    """Require local HEAD to exactly match fetched origin/main before release."""
    head_sha = git_rev_parse(root, "HEAD")
    origin_main_ref = f"refs/remotes/{RELEASE_REMOTE}/{RELEASE_BRANCH}"
    origin_main_sha = git_rev_parse(root, origin_main_ref)
    if head_sha != origin_main_sha:
        raise ReleasePrepError(
            f"Release must start from {RELEASE_REMOTE}/{RELEASE_BRANCH}.\n"
            f"HEAD: {head_sha}\n"
            f"{RELEASE_REMOTE}/{RELEASE_BRANCH}: {origin_main_sha}"
        )


def remote_main_sha(root: Path) -> str:
    """Read origin/main's current SHA without updating local refs."""
    result = run_optional_command(
        [
            "git",
            "ls-remote",
            "--exit-code",
            RELEASE_REMOTE,
            f"refs/heads/{RELEASE_BRANCH}",
        ],
        cwd=root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not resolve {RELEASE_REMOTE}/{RELEASE_BRANCH} without fetching:\n{detail}"
        )

    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    parts = first_line.split()
    if not parts:
        raise ReleasePrepError(f"Could not parse {RELEASE_REMOTE}/{RELEASE_BRANCH} SHA")
    return parts[0]


def assert_head_matches_remote_main(root: Path) -> None:
    """Require local HEAD to match remote origin/main without mutating refs."""
    head_sha = git_rev_parse(root, "HEAD")
    origin_main_sha = remote_main_sha(root)
    if head_sha != origin_main_sha:
        raise ReleasePrepError(
            f"Release must start from current remote {RELEASE_REMOTE}/{RELEASE_BRANCH}.\n"
            f"HEAD: {head_sha}\n"
            f"{RELEASE_REMOTE}/{RELEASE_BRANCH}: {origin_main_sha}"
        )


def assert_local_tag_absent(root: Path, tag_name: str) -> None:
    """Fail if the release tag already exists locally."""
    result = run_optional_command(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag_name}"],
        cwd=root,
    )
    if result.returncode == 0:
        raise ReleasePrepError(f"Local tag already exists: {tag_name}")


def assert_remote_tag_absent(root: Path, tag_name: str) -> None:
    """Fail if the release tag already exists on origin."""
    result = run_optional_command(
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--tags",
            RELEASE_REMOTE,
            f"refs/tags/{tag_name}",
        ],
        cwd=root,
    )
    if result.returncode == 0:
        raise ReleasePrepError(
            f"Remote tag already exists on {RELEASE_REMOTE}: {tag_name}"
        )
    if result.returncode != 2:
        printable = (
            f"git ls-remote --exit-code --tags {RELEASE_REMOTE} refs/tags/{tag_name}"
        )
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleasePrepError(
            f"Could not check remote tag availability: {printable}\n{detail}"
        )


def assert_github_release_absent(root: Path, tag_name: str) -> None:
    """Fail if GitHub already has a release for this tag."""
    auth_result = run_optional_command(["gh", "auth", "status"], cwd=root)
    if auth_result.returncode != 0:
        detail = auth_result.stderr.strip() or auth_result.stdout.strip()
        raise ReleasePrepError(f"GitHub CLI is not authenticated:\n{detail}")

    release_result = run_optional_command(["gh", "release", "view", tag_name], cwd=root)
    if release_result.returncode == 0:
        raise ReleasePrepError(f"GitHub release already exists: {tag_name}")


def confirm_release(
    *,
    tag_name: str,
    input_func: Callable[[str], str] = input,
) -> None:
    """Ask the operator for an exact tag confirmation."""
    try:
        answer = input_func(f"Type {tag_name} to release: ").strip()
    except EOFError as exc:
        raise ReleasePrepError(
            "Release confirmation required. Re-run with --yes to skip the prompt."
        ) from exc
    if answer != tag_name:
        raise ReleasePrepError(
            f"Release cancelled; expected exact confirmation {tag_name!r}, got {answer!r}"
        )


def print_release_plan(
    *,
    tag_name: str,
    current_project_version: str,
    current_lock_version: str,
    target_version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Print the release plan before confirmation."""
    print(f"Preparing full {PROJECT_NAME} release {tag_name}")
    print(f"Current pyproject version: {current_project_version}")
    print(f"Current uv.lock version: {current_lock_version}")
    print(f"Release source: {RELEASE_REMOTE}/{RELEASE_BRANCH}")
    print("")
    print("Plan:")
    print(
        f"  1. Require clean local {RELEASE_BRANCH} matching fetched {RELEASE_REMOTE}/{RELEASE_BRANCH}"
    )
    print(
        f"  2. Verify pyproject.toml and uv.lock already contain {target_version}"
    )
    if options.sync_environment:
        print("  3. Sync .venv and verify installed package metadata")
    else:
        print("  3. Skip .venv sync and installed metadata verification")
    if options.skip_validation:
        print("  4. Skip validation")
    else:
        print(f"  4. Run validation: {' '.join(options.validation_command)}")
    print("  5. Re-check that the git worktree is clean")
    print(f"  6. Create annotated tag {tag_name}")
    if options.push:
        print(f"  7. Push only tag {tag_name} to {RELEASE_REMOTE}")
    else:
        print("  7. Leave tag local")
    if options.create_github_release:
        print(f"  8. Create GitHub release {tag_name} with generated notes")
    else:
        print("  8. Skip GitHub release creation")


def print_dry_run_commands(
    *,
    tag_name: str,
    target_version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Print the commands the full release would run."""
    print("")
    print("Dry run: no files, git refs, or GitHub releases will be changed.")
    print("+ git status --porcelain")
    print(f"+ git remote get-url {RELEASE_REMOTE}")
    print("+ git branch --show-current")
    print("+ git rev-parse --verify HEAD")
    print(f"+ git ls-remote --exit-code {RELEASE_REMOTE} refs/heads/{RELEASE_BRANCH}")
    if options.push:
        print(
            f"+ git ls-remote --exit-code --tags {RELEASE_REMOTE} refs/tags/{tag_name}"
        )
    if options.create_github_release:
        print("+ gh auth status")
        print(f"+ gh release view {tag_name}")
    print(f"Would verify pyproject.toml and uv.lock already contain {target_version}")
    if options.sync_environment:
        print("+ uv sync --frozen --all-extras")
    if not options.skip_validation:
        print("+ " + " ".join(shlex.quote(part) for part in options.validation_command))
    print("+ git status --porcelain")
    print(f"+ git tag -a {tag_name} -m 'Release {tag_name}'")
    if options.push:
        print(
            f"+ git push {RELEASE_REMOTE} refs/tags/{tag_name}:refs/tags/{tag_name}"
        )
    if options.create_github_release:
        print(f"+ gh release create {tag_name} --generate-notes")


def run_release_preflight(
    *,
    paths: ReleasePaths,
    tag_name: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Run fail-fast checks before asking for release confirmation."""
    assert_tool_available("git")
    if options.create_github_release:
        assert_tool_available("gh")
    if options.sync_environment:
        find_uv(options.uv_executable)
    assert_clean_worktree(paths.root)
    assert_origin_remote_exists(paths.root)
    fetch_origin_main(paths.root)
    assert_current_branch_is_main(paths.root)
    assert_head_matches_origin_main(paths.root)
    assert_local_tag_absent(paths.root, tag_name)
    if options.push:
        assert_remote_tag_absent(paths.root, tag_name)
    if options.create_github_release:
        assert_github_release_absent(paths.root, tag_name)


def run_release_dry_run_preflight(
    *,
    paths: ReleasePaths,
    tag_name: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Run read-only checks that prove whether release can start."""
    assert_tool_available("git")
    if options.create_github_release:
        assert_tool_available("gh")
    if options.sync_environment:
        find_uv(options.uv_executable)
    assert_clean_worktree(paths.root)
    assert_origin_remote_exists(paths.root)
    assert_current_branch_is_main(paths.root)
    assert_head_matches_remote_main(paths.root)
    assert_local_tag_absent(paths.root, tag_name)
    if options.push:
        assert_remote_tag_absent(paths.root, tag_name)
    if options.create_github_release:
        assert_github_release_absent(paths.root, tag_name)


def apply_release_metadata(
    *,
    paths: ReleasePaths,
    target_version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Update version files, refresh the lockfile, and verify metadata."""
    uv = find_uv(options.uv_executable)
    previous_version = write_project_version(paths.pyproject, target_version)
    if previous_version == target_version:
        print(f"pyproject.toml already at {target_version}")
    else:
        print(f"Updated pyproject.toml: {previous_version} -> {target_version}")

    run_command([uv, "lock"], cwd=paths.root)
    verify_project_and_lock_versions(paths, target_version)
    sync_environment_if_requested(
        paths=paths,
        expected_version=target_version,
        options=options,
    )


def verify_release_metadata_ready(
    paths: ReleasePaths, target_version: str
) -> None:
    """Require the already-merged release metadata to match the target version."""
    try:
        verify_project_and_lock_versions(paths, target_version)
    except ReleasePrepError as exc:
        raise ReleasePrepError(
            "Release version must already be present in pyproject.toml and uv.lock. "
            f"Run `make prepare-release VERSION=v{target_version}`, merge that PR "
            f"into {RELEASE_BRANCH}, then run release from the updated "
            f"{RELEASE_BRANCH} branch.\n{exc}"
        ) from exc
    print(f"Version {target_version} already present in pyproject.toml and uv.lock.")


def sync_environment_if_requested(
    *,
    paths: ReleasePaths,
    expected_version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Sync the local environment before verifying installed package metadata."""
    if not options.sync_environment:
        print(
            "Skipped environment sync; run uv sync --frozen --all-extras before opening Control Center."
        )
        return

    uv = find_uv(options.uv_executable)
    run_command([uv, "sync", "--frozen", "--all-extras"], cwd=paths.root)
    verify_installed_package_version(paths, expected_version)


def run_release_validation(
    paths: ReleasePaths, options: ReleaseWorkflowOptions
) -> None:
    """Run the configured release validation phase."""
    if options.skip_validation:
        print("Skipped validation.")
        return
    run_command(options.validation_command, cwd=paths.root)


def create_release_tag(paths: ReleasePaths, tag_name: str) -> None:
    """Create the annotated release tag on the verified release commit."""
    assert_local_tag_absent(paths.root, tag_name)
    run_command(
        ["git", "tag", "-a", tag_name, "-m", f"Release {tag_name}"], cwd=paths.root
    )


def publish_release(
    paths: ReleasePaths, tag_name: str, options: ReleaseWorkflowOptions
) -> None:
    """Push the release tag and create the GitHub release when enabled."""
    if options.push:
        run_command(
            [
                "git",
                "push",
                RELEASE_REMOTE,
                f"refs/tags/{tag_name}:refs/tags/{tag_name}",
            ],
            cwd=paths.root,
        )
    else:
        print("Skipped push; tag remains local.")

    if options.create_github_release:
        run_command(
            ["gh", "release", "create", tag_name, "--generate-notes"], cwd=paths.root
        )
    else:
        print("Skipped GitHub release creation.")


def run_release_workflow(
    *,
    paths: ReleasePaths,
    version: str,
    options: ReleaseWorkflowOptions,
) -> None:
    """Run the full confirmed release workflow."""
    if not options.push and options.create_github_release:
        raise ReleasePrepError("--local-only cannot create a GitHub release")

    target_version = normalize_release_version(version)
    tag_name = f"v{target_version}"
    current_project_version = read_project_version(paths.pyproject)
    current_lock_version = read_lock_project_version(paths.lockfile)

    print_release_plan(
        tag_name=tag_name,
        current_project_version=current_project_version,
        current_lock_version=current_lock_version,
        target_version=target_version,
        options=options,
    )

    if options.dry_run:
        run_release_dry_run_preflight(paths=paths, tag_name=tag_name, options=options)
        verify_release_metadata_ready(paths, target_version)
        print_dry_run_commands(
            tag_name=tag_name,
            target_version=target_version,
            options=options,
        )
        return

    run_release_preflight(paths=paths, tag_name=tag_name, options=options)
    verify_release_metadata_ready(paths, target_version)
    if not options.assume_yes:
        confirm_release(tag_name=tag_name)

    sync_environment_if_requested(
        paths=paths,
        expected_version=target_version,
        options=options,
    )
    run_release_validation(paths, options)
    assert_clean_worktree(paths.root)
    create_release_tag(paths, tag_name)
    publish_release(paths, tag_name, options)
    print("")
    print(f"Release complete: {tag_name}")


def prepare_release(
    *,
    paths: ReleasePaths,
    version: str,
    dry_run: bool,
    sync_environment: bool,
    uv_executable: str | None,
) -> None:
    """Prepare the release bump and verify the version sources."""
    target_version = normalize_release_version(version)
    tag_name = f"v{target_version}"
    current_project_version = read_project_version(paths.pyproject)
    current_lock_version = read_lock_project_version(paths.lockfile)

    print(f"Preparing {PROJECT_NAME} release {tag_name}")
    print(f"Current pyproject version: {current_project_version}")
    print(f"Current uv.lock version: {current_lock_version}")

    if dry_run:
        print(f"Dry run: would set pyproject.toml and uv.lock to {target_version}")
        if sync_environment:
            print("Dry run: would run uv sync --frozen --all-extras")
        return

    apply_release_metadata(
        paths=paths,
        target_version=target_version,
        options=ReleaseWorkflowOptions(
            dry_run=False,
            sync_environment=sync_environment,
            assume_yes=False,
            skip_validation=True,
            validation_command=(),
            push=False,
            create_github_release=False,
            uv_executable=uv_executable,
        ),
    )

    print("")
    print(f"Release file prep complete for {tag_name}.")
    print(
        f"Merge these files to {RELEASE_BRANCH}, then run `make release VERSION={tag_name}` "
        f"from the updated clean {RELEASE_BRANCH} branch."
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the issue-orchestrator release workflow. By default this asks "
            "for confirmation, verifies release metadata already exists on "
            "main, validates, tags, pushes the tag, and creates the GitHub release."
        )
    )
    parser.add_argument("version", help="Release version, e.g. 1.0.0 or v1.0.0")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root. Defaults to this script's parent repo.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Only bump pyproject.toml, refresh uv.lock, and verify local metadata.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without changing files, git refs, or GitHub releases.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not run uv sync or verify local .venv package metadata.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive release confirmation prompt.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Do not run the release validation command.",
    )
    parser.add_argument(
        "--validation-command",
        default=" ".join(DEFAULT_VALIDATION_COMMAND),
        help="Validation command to run before tagging. Default: make validate-pr.",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Tag locally, but do not push or create a GitHub release.",
    )
    parser.add_argument(
        "--no-gh-release",
        action="store_true",
        help="Push the tag but skip gh release create.",
    )
    parser.add_argument(
        "--uv",
        help="Path to uv executable. Defaults to UV env var or uv on PATH.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = ReleasePaths.from_root(args.project_root)
        if args.prepare_only:
            prepare_release(
                paths=paths,
                version=args.version,
                dry_run=args.dry_run,
                sync_environment=not args.no_sync,
                uv_executable=args.uv,
            )
        else:
            run_release_workflow(
                paths=paths,
                version=args.version,
                options=ReleaseWorkflowOptions(
                    dry_run=args.dry_run,
                    sync_environment=not args.no_sync,
                    assume_yes=args.yes,
                    skip_validation=args.skip_validation,
                    validation_command=parse_command(args.validation_command),
                    push=not args.local_only,
                    create_github_release=not args.local_only
                    and not args.no_gh_release,
                    uv_executable=args.uv,
                ),
            )
    except ReleasePrepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
