"""Porchpin's oversized registry can be recovered without hand-editing objects.

#7272 left one repository with a 104 787-byte record living in a commit message
that GitHub will only ever return the first 65 536 characters of. The object is
intact in git, so recovery is possible -- but only if it is done with git, not
with the API read that is the thing that is broken.

These tests run the real script against real repositories on disk, because the
only claim worth making here is that the plumbing works.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.github.pattern_registry import (
    PATTERN_REGISTRY_REF_KEY,
    PATTERN_REGISTRY_REF_PREFIX,
    parse_entries,
)
from issue_orchestrator.adapters.github.ref_store import RECORD_PATH
from tests.registry_records import PORCHPIN_PATTERN_COUNT, registry_record

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "republish_pattern_registry_record.py"
)
REF = f"{PATTERN_REGISTRY_REF_PREFIX}/{PATTERN_REGISTRY_REF_KEY}"


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare repository standing in for GitHub, with one commit on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main", ".")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    return origin


@pytest.fixture
def clone(tmp_path: Path, remote: Path) -> Path:
    workspace = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(workspace))
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    return workspace


def _seed_legacy_ref(clone: Path, remote: Path, record: str) -> None:
    """Publish a record the pre-#7272 way: as the commit message itself."""
    base = _git(clone, "rev-parse", "HEAD").strip()
    tree = _git(clone, "rev-parse", "HEAD^{tree}").strip()
    commit = _git(
        clone, "commit-tree", tree, "-p", base, "-F", "-", stdin=record.encode()
    ).strip()
    _git(clone, "push", "origin", f"{commit}:{REF}")


def _run(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(clone), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _published_record(clone: Path) -> str:
    _git(clone, "fetch", "--no-tags", "origin", REF)
    head = _git(clone, "rev-parse", "FETCH_HEAD").strip()
    listing = _git(clone, "ls-tree", f"{head}^{{tree}}")
    blob = next(
        line.split()[2]
        for line in listing.splitlines()
        if line.partition("\t")[2] == RECORD_PATH
    )
    return _git(clone, "cat-file", "blob", blob)


class TestRecovery:
    def test_an_oversized_message_record_is_republished_with_every_entry(
        self, clone: Path, remote: Path
    ) -> None:
        record = registry_record(PORCHPIN_PATTERN_COUNT)
        assert len(record) > 65536, "the fixture must cross the API's cap"
        _seed_legacy_ref(clone, remote, record)

        result = _run(clone, "--apply")

        assert result.returncode == 0, result.stderr
        assert f"republished ({PORCHPIN_PATTERN_COUNT} entries)" in result.stdout
        assert parse_entries(_published_record(clone)) == parse_entries(record)

    def test_the_recovered_commit_descends_from_the_one_it_replaces(
        self, clone: Path, remote: Path
    ) -> None:
        """A fast-forward, so a concurrent writer is never clobbered."""
        _seed_legacy_ref(clone, remote, registry_record(3))
        _git(clone, "fetch", "--no-tags", "origin", REF)
        before = _git(clone, "rev-parse", "FETCH_HEAD").strip()

        assert _run(clone, "--apply").returncode == 0

        _git(clone, "fetch", "--no-tags", "origin", REF)
        after = _git(clone, "rev-parse", "FETCH_HEAD").strip()
        assert _git(clone, "rev-parse", f"{after}^").strip() == before

    def test_running_it_again_is_a_no_op(self, clone: Path, remote: Path) -> None:
        _seed_legacy_ref(clone, remote, registry_record(3))
        assert _run(clone, "--apply").returncode == 0
        _git(clone, "fetch", "--no-tags", "origin", REF)
        after_first = _git(clone, "rev-parse", "FETCH_HEAD").strip()

        second = _run(clone, "--apply")

        assert second.returncode == 0
        assert "already blob-backed (3 entries)" in second.stdout
        _git(clone, "fetch", "--no-tags", "origin", REF)
        assert _git(clone, "rev-parse", "FETCH_HEAD").strip() == after_first


class TestInspection:
    def test_a_dry_run_reports_the_record_without_publishing_it(
        self, clone: Path, remote: Path
    ) -> None:
        _seed_legacy_ref(clone, remote, registry_record(PORCHPIN_PATTERN_COUNT))
        _git(clone, "fetch", "--no-tags", "origin", REF)
        before = _git(clone, "rev-parse", "FETCH_HEAD").strip()

        result = _run(clone)

        assert result.returncode == 0
        assert f"recoverable ({PORCHPIN_PATTERN_COUNT} entries)" in result.stdout
        _git(clone, "fetch", "--no-tags", "origin", REF)
        assert _git(clone, "rev-parse", "FETCH_HEAD").strip() == before


class TestPostPushVerification:
    def test_a_push_the_remote_rewrites_is_reported_as_a_failure(
        self, clone: Path, remote: Path
    ) -> None:
        """The record is read back from the remote, not assumed from the push.

        A remote that accepts the push and then moves the ref elsewhere would
        otherwise be reported as a successful recovery.
        """
        _seed_legacy_ref(clone, remote, registry_record(3))
        _git(clone, "fetch", "--no-tags", "origin", REF)
        before = _git(clone, "rev-parse", "FETCH_HEAD").strip()
        hook = remote / "hooks" / "post-receive"
        hook.write_text(
            f'#!/bin/sh\nexec git update-ref {REF} {before}\n'
        )
        hook.chmod(0o755)

        result = _run(clone, "--apply")

        assert result.returncode == 1
        assert "after the push" in result.stderr


class TestRefusals:
    def test_a_record_this_clone_cannot_read_is_refused(
        self, clone: Path, remote: Path
    ) -> None:
        """A local object that is ALSO short has nothing to recover from."""
        intact = registry_record(4)
        _seed_legacy_ref(clone, remote, intact[: len(intact) // 2])

        result = _run(clone, "--apply")

        assert result.returncode == 1
        assert "could not be read" in result.stderr

    def test_a_missing_ref_fails_rather_than_inventing_an_empty_registry(
        self, clone: Path
    ) -> None:
        result = _run(clone, "--apply")

        assert result.returncode == 1
        assert "FAILED" in result.stderr
