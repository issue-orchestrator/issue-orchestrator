"""Tests for the CC source-snapshot helper.

The helper freezes the orchestrator's ``src/`` tree at CC launch so
subsequent base-repo branch changes cannot leak into running agent
sessions (issue #5950). The tests pin the invariants the shell script
depends on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.infra.cc_snapshot import (
    SNAPSHOT_DIR_NAME,
    SOURCE_METADATA_FILE,
    clean_snapshots,
    create_snapshot,
    snapshot_root,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a minimal fake repo with a ``src/`` tree to snapshot."""
    src = tmp_path / "src" / "issue_orchestrator"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("__version__ = '0.0.0-test'\n")
    (src / "marker.txt").write_text("live\n")
    return tmp_path


class TestCreateSnapshot:
    def test_freezes_src_tree_into_new_dir(self, repo: Path) -> None:
        snapshot_dir = create_snapshot(repo)

        assert snapshot_dir.parent == snapshot_root(repo)
        assert snapshot_dir.parent.name == SNAPSHOT_DIR_NAME
        assert snapshot_dir.name.startswith("launch-")
        frozen = snapshot_dir / "src" / "issue_orchestrator" / "marker.txt"
        assert frozen.read_text() == "live\n"

    def test_snapshot_is_a_copy_not_a_link(self, repo: Path) -> None:
        """Mutations to the base repo after snapshot must not leak in.

        This is the single invariant the whole fix rests on: if the
        snapshot were a symlink, a branch switch in the base repo would
        immediately change what agents import — reintroducing the bug.
        """
        snapshot_dir = create_snapshot(repo)

        (repo / "src" / "issue_orchestrator" / "marker.txt").write_text("mutated\n")

        frozen = snapshot_dir / "src" / "issue_orchestrator" / "marker.txt"
        assert frozen.read_text() == "live\n"
        assert not frozen.is_symlink()

    def test_packaged_brand_assets_are_in_src_snapshot(self, repo: Path) -> None:
        """Runtime UI assets must live under ``src/`` so the frozen CC can serve them."""
        brand = repo / "src" / "issue_orchestrator" / "static" / "brand"
        brand.mkdir(parents=True)
        (brand / "logo.svg").write_text("<svg>logo</svg>\n")
        (brand / "tray-icon.png").write_bytes(b"png")

        snapshot_dir = create_snapshot(repo)

        frozen_brand = snapshot_dir / "src" / "issue_orchestrator" / "static" / "brand"
        assert (frozen_brand / "logo.svg").read_text() == "<svg>logo</svg>\n"
        assert (frozen_brand / "tray-icon.png").read_bytes() == b"png"

    def test_completion_wrapper_helper_is_in_src_snapshot(self, repo: Path) -> None:
        scripts = repo / "src" / "issue_orchestrator" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "completion-wrapper-lib.sh").write_text("#!/bin/bash\n")

        snapshot_dir = create_snapshot(repo)

        frozen_helper = (
            snapshot_dir
            / "src"
            / "issue_orchestrator"
            / "scripts"
            / "completion-wrapper-lib.sh"
        )
        assert frozen_helper.read_text() == "#!/bin/bash\n"

    def test_snapshot_records_source_commit_sha(self, repo: Path) -> None:
        """The frozen snapshot carries the source identity used by the CC footer."""
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "src"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-q",
                "-m",
                "initial",
            ],
            cwd=repo,
            check=True,
        )
        expected_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
        ).strip()

        snapshot_dir = create_snapshot(repo)

        metadata = json.loads((snapshot_dir / SOURCE_METADATA_FILE).read_text(encoding="utf-8"))
        assert metadata["schema_version"] == 1
        assert metadata["source_repo_root"] == str(repo.resolve())
        assert metadata["commit_sha"] == expected_sha

    def test_fails_fast_when_no_src_tree(self, tmp_path: Path) -> None:
        """Without a ``src/`` tree there is nothing to freeze; caller
        must not silently produce an empty snapshot."""
        with pytest.raises(FileNotFoundError, match="Source tree not found"):
            create_snapshot(tmp_path)

    def test_collision_on_same_millisecond_is_resolved(self, repo: Path) -> None:
        """Two snapshots taken in the same millisecond get distinct dirs.

        The shell script always calls stop_all_orchestrators first so
        this is rare in production, but tests hammer the function in
        tight loops and we must never clobber a sibling snapshot.
        """
        now = 1_700_000_000.123
        a = create_snapshot(repo, now=now)
        b = create_snapshot(repo, now=now)

        assert a != b
        assert a.parent == b.parent


class TestCleanSnapshots:
    def test_removes_every_snapshot_dir(self, repo: Path) -> None:
        """``stop_all_orchestrators`` has already killed every CC before
        the shell script calls ``clean``; all snapshots are therefore
        orphans and must be removed."""
        first = create_snapshot(repo, now=1.0)
        second = create_snapshot(repo, now=2.0)

        removed = clean_snapshots(repo)

        assert set(removed) == {first, second}
        assert not first.exists()
        assert not second.exists()
        # Parent dir survives; only its contents are purged.
        assert snapshot_root(repo).exists()

    def test_no_snapshot_dir_is_a_noop(self, tmp_path: Path) -> None:
        """Fresh repo — nothing to clean, must not raise."""
        assert clean_snapshots(tmp_path) == []

    def test_skips_snapshot_owned_by_live_pid(self, repo: Path) -> None:
        """Second line of defence: if a stray ``clean`` fires while a
        CC is running, the live CC's snapshot must not be torn out
        from under it. The shell script writes the CC's PID into
        ``cc.pid`` after creation; ``clean`` honours it.
        """
        import os

        live = create_snapshot(repo, now=1.0)
        dead = create_snapshot(repo, now=2.0)
        # Our own PID is unambiguously live.
        (live / "cc.pid").write_text(f"{os.getpid()}\n")
        # A PID that was never in use: 1 is init, but 0 / negative /
        # out-of-range PIDs are safer stand-ins. We pick a very high
        # PID that is astronomically unlikely to exist.
        (dead / "cc.pid").write_text("999999999\n")

        removed = clean_snapshots(repo)

        assert live.exists(), "live-PID snapshot must survive cleanup"
        assert not dead.exists(), "dead-PID snapshot must be removed"
        assert removed == [dead]

    def test_missing_pid_marker_treated_as_orphan(self, repo: Path) -> None:
        """Historical behaviour: a snapshot dir without a PID marker
        is orphaned (created by a previous version of the script, or
        by a CC that crashed before exec). Clean it."""
        orphan = create_snapshot(repo, now=1.0)
        assert not (orphan / "cc.pid").exists()

        removed = clean_snapshots(repo)

        assert removed == [orphan]

    def test_malformed_pid_marker_treated_as_orphan(self, repo: Path) -> None:
        """Corruption or external tampering of ``cc.pid`` must fall
        through to cleanup — otherwise a single bad write to the marker
        could lock a snapshot dir undeletable forever."""
        bad = create_snapshot(repo, now=1.0)
        (bad / "cc.pid").write_text("not-a-number\n")

        removed = clean_snapshots(repo)

        assert removed == [bad]


class TestCli:
    """Integration-adjacent: exercise the ``python -m`` contract the
    shell script depends on."""

    def test_create_prints_pythonpath_entry_on_stdout(self, repo: Path) -> None:
        """The shell script captures stdout and prepends it to PYTHONPATH.
        Any extra chatter on stdout would corrupt the env var; logs go
        to stderr instead.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.infra.cc_snapshot",
                "create",
                "--root",
                str(repo),
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        stdout_lines = [line for line in result.stdout.splitlines() if line]
        assert len(stdout_lines) == 1, (
            f"expected exactly one line on stdout, got: {result.stdout!r}"
        )
        pythonpath_entry = Path(stdout_lines[0])
        assert pythonpath_entry.is_dir()
        assert pythonpath_entry.name == "src"
        # Re-import path must actually resolve the package.
        assert (pythonpath_entry / "issue_orchestrator" / "__init__.py").is_file()

    def test_create_cleans_previous_snapshots(self, repo: Path) -> None:
        """Each ``create`` starts by wiping prior snapshots — the shell
        script calls this right after ``stop_all_orchestrators``, so
        prior dirs are guaranteed orphans."""
        stale = create_snapshot(repo, now=1.0)
        assert stale.exists()

        subprocess.run(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.infra.cc_snapshot",
                "create",
                "--root",
                str(repo),
            ],
            capture_output=True,
            check=True,
        )

        assert not stale.exists()
        # The new snapshot is the sole survivor.
        live = [p for p in snapshot_root(repo).iterdir() if p.is_dir()]
        assert len(live) == 1

    def test_clean_command_independent_of_create(self, repo: Path) -> None:
        create_snapshot(repo, now=1.0)
        create_snapshot(repo, now=2.0)

        subprocess.run(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.infra.cc_snapshot",
                "clean",
                "--root",
                str(repo),
            ],
            capture_output=True,
            check=True,
        )

        assert list(snapshot_root(repo).iterdir()) == []
