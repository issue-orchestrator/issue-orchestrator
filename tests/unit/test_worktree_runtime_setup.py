"""Tests for the worktree runtime setup owner.

These pin two things the lifecycle module used to own inline:

1. Applying setup to a worktree produces a *complete* runnable state, and says
   so through a typed result rather than leaving callers to infer it.
2. The failure semantics of each step. Runtime setup used to degrade silently
   (a phantom worktree identity, a dropped ``--no-verify`` flag, a settings
   file replaced without a word), which turned a broken worktree into a
   confusing session failure much later.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.adapters.worktree.api import (
    WorktreeError,
    WorktreeRuntimeSetup,
    install_claude_settings,
)
from issue_orchestrator.adapters.worktree._worktree_runtime import (
    ALLOW_NO_VERIFY_DRY_RUN_PATH,
    CLAUDE_SETTINGS_FOR_AGENTS,
)
from issue_orchestrator.adapters.worktree._worktree_legacy_cli_tools import (
    LEGACY_CLI_TOOLS_DROP_DIR,
    LegacyDropRetirement,
)
from issue_orchestrator.ports.worktree_manager import WORKTREE_ID_MARKER
from tests.unit.worktree_git_helpers import (
    block_worktree_config_writes,
    effective_hooks_path,
    make_git_worktree,
)


def _break_read_of(
    monkeypatch: pytest.MonkeyPatch, target: Path, error: OSError
) -> None:
    """Make exactly one path fail to read while its bytes stay intact on disk.

    This is the case the owner has to tell apart from "the content is broken",
    and it cannot be staged with ``chmod`` alone — a root test runner reads a
    ``0o000`` file happily, so the test would pass for the wrong reason there
    and fail for the wrong reason here. Only ``target`` is affected; every other
    read in the setup sequence runs for real.
    """
    original_read_text = Path.read_text

    def guarded(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise error
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    return root


@pytest.fixture
def worktree_path(tmp_path: Path, repo_root: Path) -> Path:
    """A worktree linked to ``repo_root`` the way ``git worktree add`` links it."""
    path = tmp_path / "repo-123"
    path.mkdir()
    gitdir = repo_root / ".git" / "worktrees" / "repo-123"
    gitdir.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {gitdir}")
    return path


def _setup(repo_root: Path, **overrides) -> WorktreeRuntimeSetup:
    options = {"enforce_hooks": False}
    options.update(overrides)
    return WorktreeRuntimeSetup(repo_root=repo_root, **options)


class TestApplyProducesRunnableWorktree:
    """One call must leave the worktree ready for an agent session."""

    def test_apply_installs_every_runtime_artifact(self, repo_root, worktree_path):
        state = _setup(repo_root).apply(worktree_path)

        assert (worktree_path / ".claude" / "settings.json").exists()
        assert (worktree_path / WORKTREE_ID_MARKER).read_text() == state.worktree_id
        # A worktree owns its environment: runtime setup must not plant a
        # venv symlinked at the repo's, which is how the editable pointer
        # used to be rewritten by whichever checkout synced last.
        assert not (worktree_path / ".venv").exists()
        # io's tooling resolves from the session environment, never from
        # files placed in the target's tree (#7566).
        assert not (worktree_path / "src").exists()
        assert state.legacy_drop_retirement == LegacyDropRetirement()

    def test_apply_hides_runtime_artifacts_from_git_status(
        self, repo_root, worktree_path
    ):
        _setup(repo_root).apply(worktree_path)

        exclude_text = (
            repo_root / ".git" / "worktrees" / "repo-123" / "info" / "exclude"
        ).read_text()
        assert ".claude/settings.json" in exclude_text
        assert str(WORKTREE_ID_MARKER) in exclude_text
        assert "src/issue_orchestrator" not in exclude_text

    def test_apply_reports_what_it_did(self, repo_root, worktree_path):
        state = _setup(
            repo_root, enforce_hooks=False, allow_no_verify_dry_run_preflight=True
        ).apply(worktree_path)

        assert state.worktree_path == worktree_path
        assert state.worktree_id.startswith("wt-")
        assert state.hooks_installed is False
        assert state.no_verify_dry_run_allowed is True

    def test_apply_is_idempotent_and_keeps_worktree_identity(
        self, repo_root, worktree_path
    ):
        setup = _setup(repo_root)

        first = setup.apply(worktree_path)
        second = setup.apply(worktree_path)

        assert second.worktree_id == first.worktree_id

    def test_hooks_are_installed_when_enforced(self, tmp_path):
        # Real repo: "installed" means git will run it, which only a real repo
        # can be asked.
        wt = make_git_worktree(tmp_path)

        state = WorktreeRuntimeSetup(
            repo_root=wt.main_repo, enforce_hooks=True
        ).apply(wt.worktree_path)

        assert state.hooks_installed is True
        assert (wt.hooks_dir / "pre-push").exists()
        assert effective_hooks_path(wt.worktree_path) == str(wt.hooks_dir)

    def test_hooks_are_skipped_when_not_enforced(self, repo_root, worktree_path):
        state = WorktreeRuntimeSetup(repo_root=repo_root, enforce_hooks=False).apply(
            worktree_path
        )

        assert state.hooks_installed is False
        assert not (
            repo_root / ".git" / "worktrees" / "repo-123" / "hooks" / "pre-push"
        ).exists()


def _git_out(worktree: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=worktree, check=True, capture_output=True, text=True
    ).stdout


def _visible_to_target_tooling(worktree: Path) -> list[str]:
    """Every non-clean path git knows of, ignored files included.

    ``--ignored`` is the point: an ``info/exclude`` entry hides a file from
    ``git status`` but not from a validator that walks the tree.
    """
    return _git_out(
        worktree, "status", "--porcelain", "--ignored", "--untracked-files=all"
    ).splitlines()


def _commit_io_cli_tool(wt, content: str) -> Path:
    """Make ``wt`` look like io's own repo: cli_tools is tracked source."""
    tool = wt.worktree_path / LEGACY_CLI_TOOLS_DROP_DIR / "coding_done.py"
    tool.parent.mkdir(parents=True)
    tool.write_text(content)
    _git_out(wt.worktree_path, "add", str(tool))
    _git_out(
        wt.worktree_path,
        "-c", "user.email=t@example.com", "-c", "user.name=T",
        "commit", "-m", "io source",
    )
    return tool


class TestIoToolingStaysOutOfTheTargetTree:
    """io's completion tooling must never land in a target's worktree (#7566).

    porchpin's architecture audit failed on a ``src/issue_orchestrator/`` tree
    io planted (hidden from ``git status`` by ``info/exclude``, but not from a
    validator walking the tree). Completion commands resolve from the session
    environment instead; see ``test_session_env``.
    """

    def test_foreign_worktree_holds_no_io_tooling(self, tmp_path):
        wt = make_git_worktree(tmp_path)

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert not (wt.worktree_path / "src").exists()
        visible = _visible_to_target_tooling(wt.worktree_path)
        assert not [line for line in visible if "issue_orchestrator" in line]
        assert state.legacy_drop_retirement == LegacyDropRetirement()

    def test_io_repo_worktree_keeps_its_committed_source(self, tmp_path):
        """In io's own repo, ``cli_tools`` is the project under review.

        The old drop overwrote it with the engine's copy and set
        ``skip-worktree``, so tests ran against code that was not the branch's
        while git reported a clean tree.
        """
        wt = make_git_worktree(tmp_path)
        tool = _commit_io_cli_tool(wt, "BRANCH_VERSION = True\n")

        _setup(wt.main_repo).apply(wt.worktree_path)

        assert tool.read_text() == "BRANCH_VERSION = True\n"
        tag = _git_out(wt.worktree_path, "ls-files", "-v", "--", str(tool))
        assert tag.startswith("H "), tag
        assert _git_out(wt.worktree_path, "status", "--porcelain") == ""


LEGACY_TOOL = LEGACY_CLI_TOOLS_DROP_DIR / "coding_done.py"


def _plant_legacy_foreign_drop(wt, worktree: Path | None = None) -> Path:
    """Reproduce what pre-#7566 setup left in a foreign worktree."""
    worktree = worktree or wt.worktree_path
    planted = worktree / LEGACY_TOOL
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("ENGINE_COPY = True\n")
    _write_legacy_exclude_line(wt)
    return planted


def _write_legacy_exclude_line(wt) -> None:
    common_exclude = wt.main_repo / ".git" / "info" / "exclude"
    common_exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = common_exclude.read_text() if common_exclude.exists() else ""
    if LEGACY_TOOL.as_posix() not in existing:
        common_exclude.write_text(f"{existing}{LEGACY_TOOL.as_posix()}\n")


def _untracked(worktree: Path) -> list[str]:
    return _git_out(
        worktree, "ls-files", "--others", "--exclude-standard"
    ).splitlines()


class TestLegacyDropIsRetiredOnReuse:
    """A worktree provisioned before #7566 still carries the drop.

    Ownership is proved (an exact io exclude line, or io's skip-worktree bit),
    and nothing is deleted: bytes leave the tree for a quarantine directory in
    the common git dir, outside every worktree.
    """

    def test_foreign_drop_leaves_the_tree_into_quarantine(self, tmp_path):
        wt = make_git_worktree(tmp_path)
        _plant_legacy_foreign_drop(wt)
        agent_file = wt.worktree_path / "notes.md"
        agent_file.write_text("mine\n")

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        retirement = state.legacy_drop_retirement
        assert not (wt.worktree_path / "src").exists()
        assert agent_file.read_text() == "mine\n"
        assert retirement.quarantined == (LEGACY_TOOL,)
        assert retirement.quarantine_dir is not None
        assert retirement.quarantine_dir.is_relative_to(wt.main_repo / ".git")
        assert (retirement.quarantine_dir / LEGACY_TOOL).read_text() == "ENGINE_COPY = True\n"
        visible = _visible_to_target_tooling(wt.worktree_path)
        assert not [line for line in visible if "issue_orchestrator" in line]

    def test_target_ignored_file_io_never_listed_is_left_in_place(self, tmp_path):
        """A target ``.gitignore`` rule is not io's ownership proof."""
        wt = make_git_worktree(tmp_path)
        _plant_legacy_foreign_drop(wt)
        (wt.worktree_path / ".gitignore").write_text("cli_tools/\n")
        agent_made = wt.worktree_path / LEGACY_CLI_TOOLS_DROP_DIR / "agent_made.py"
        agent_made.write_text("mine\n")

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert agent_made.read_text() == "mine\n"
        assert not (wt.worktree_path / LEGACY_TOOL).exists()
        assert state.legacy_drop_retirement.quarantined == (LEGACY_TOOL,)

    def test_unlisted_untracked_file_under_drop_dir_is_left_alone(self, tmp_path):
        wt = make_git_worktree(tmp_path)
        drop = wt.worktree_path / LEGACY_CLI_TOOLS_DROP_DIR
        drop.mkdir(parents=True)
        (drop / "agent_made.py").write_text("mine\n")

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert (drop / "agent_made.py").read_text() == "mine\n"
        assert state.legacy_drop_retirement == LegacyDropRetirement()

    def test_io_repo_skip_worktree_snapshot_is_restored_and_its_bytes_kept(
        self, tmp_path
    ):
        """``skip-worktree`` proves io hid the file, not who wrote its bytes.

        Whatever was on disk (the engine's copy, or an agent edit git never
        saw) is preserved in quarantine before the committed content returns.
        """
        wt = make_git_worktree(tmp_path)
        tool = _commit_io_cli_tool(wt, "BRANCH_VERSION = True\n")
        tool.write_text("EDITED_AFTER_THE_DROP = True\n")
        _git_out(wt.worktree_path, "update-index", "--skip-worktree", "--", str(tool))

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        retirement = state.legacy_drop_retirement
        assert tool.read_text() == "BRANCH_VERSION = True\n"
        tag = _git_out(wt.worktree_path, "ls-files", "-v", "--", str(tool))
        assert tag.startswith("H "), tag
        assert retirement.restored == (LEGACY_TOOL,)
        assert retirement.quarantine_dir is not None
        assert (
            retirement.quarantine_dir / LEGACY_TOOL
        ).read_text() == "EDITED_AFTER_THE_DROP = True\n"

    def test_exclude_lines_go_once_no_worktree_holds_a_drop(self, tmp_path):
        """Stale exclude lines would hide an agent's new file at that path."""
        wt = make_git_worktree(tmp_path)
        _plant_legacy_foreign_drop(wt)

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert state.legacy_drop_retirement.exclude_lines_removed is True
        common_exclude = (wt.main_repo / ".git" / "info" / "exclude").read_text()
        assert LEGACY_TOOL.as_posix() not in common_exclude
        new_file = wt.worktree_path / LEGACY_TOOL
        new_file.parent.mkdir(parents=True)
        new_file.write_text("agent work\n")
        assert LEGACY_TOOL.as_posix() in _untracked(wt.worktree_path)

    def test_relative_gitdir_link_still_finds_the_common_exclude(self, tmp_path):
        """``git worktree add --relative-paths`` writes a relative ``.git`` link."""
        wt = make_git_worktree(tmp_path)
        relative = tmp_path / "wt-relative"
        _git_out(
            wt.main_repo, "worktree", "add", "--relative-paths", str(relative), "-b", "rel"
        )
        assert not (relative / ".git").read_text().split(":", 1)[1].strip().startswith("/")
        _plant_legacy_foreign_drop(wt, relative)

        state = _setup(wt.main_repo).apply(relative)

        assert state.legacy_drop_retirement.quarantined == (LEGACY_TOOL,)
        assert state.legacy_drop_retirement.exclude_lines_removed is True
        assert not (relative / "src").exists()

    def test_exclude_lines_stay_while_another_worktree_still_holds_a_drop(
        self, tmp_path
    ):
        """Another worktree may be mid-session on the old setup.

        Removing the shared lines would surface its drop as untracked and fail
        that session's completion intake.
        """
        wt = make_git_worktree(tmp_path)
        other = tmp_path / "wt-other"
        _git_out(wt.main_repo, "worktree", "add", str(other), "-b", "other")
        _plant_legacy_foreign_drop(wt)
        _plant_legacy_foreign_drop(wt, other)

        first = _setup(wt.main_repo).apply(wt.worktree_path)

        assert first.legacy_drop_retirement.exclude_lines_removed is False
        assert _untracked(other) == []
        assert (other / LEGACY_TOOL).exists()

        second = _setup(wt.main_repo).apply(other)

        assert second.legacy_drop_retirement.exclude_lines_removed is True
        assert not (other / "src").exists()
        common_exclude = (wt.main_repo / ".git" / "info" / "exclude").read_text()
        assert LEGACY_TOOL.as_posix() not in common_exclude


    def test_a_dangling_symlink_in_another_worktree_still_holds_the_lines(
        self, tmp_path
    ):
        wt = make_git_worktree(tmp_path)
        other = tmp_path / "wt-other"
        _git_out(wt.main_repo, "worktree", "add", str(other), "-b", "other")
        _write_legacy_exclude_line(wt)
        dangling = other / LEGACY_TOOL
        dangling.parent.mkdir(parents=True)
        dangling.symlink_to(tmp_path / "gone.py")

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert state.legacy_drop_retirement.exclude_lines_removed is False
        assert _untracked(other) == []


class TestLegacyRetirementLeavesOthersAlone:
    """Retirement must not disturb git's own state or concurrent setups."""

    def test_sparse_checkout_skip_worktree_bits_are_not_io_s(self, tmp_path):
        """Git sets ``skip-worktree`` on paths a sparse checkout omits."""
        wt = make_git_worktree(tmp_path)
        tool = _commit_io_cli_tool(wt, "BRANCH_VERSION = True\n")
        _git_out(wt.worktree_path, "sparse-checkout", "set", "--no-cone", "/seed")
        assert not tool.exists()
        _write_legacy_exclude_line(wt)

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert not tool.exists()
        tag = _git_out(wt.worktree_path, "ls-files", "-v", "--", str(tool))
        assert tag.startswith("S "), tag
        assert state.legacy_drop_retirement.restored == ()

    @pytest.mark.parametrize("with_legacy_line", [False, True])
    def test_symlinked_drop_path_is_the_target_s_and_left_intact(
        self, tmp_path, with_legacy_line
    ):
        """io only ever created real directories; a symlink is the target's."""
        wt = make_git_worktree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        link = wt.worktree_path / LEGACY_CLI_TOOLS_DROP_DIR
        link.parent.mkdir(parents=True)
        link.symlink_to(outside, target_is_directory=True)
        _git_out(wt.worktree_path, "add", str(link))
        _git_out(
            wt.worktree_path,
            "-c", "user.email=t@example.com", "-c", "user.name=T",
            "commit", "-m", "symlink",
        )
        if with_legacy_line:
            (outside / "coding_done.py").write_text("THE TARGET'S\n")
            _write_legacy_exclude_line(wt)

        state = _setup(wt.main_repo).apply(wt.worktree_path)

        assert link.is_symlink()
        assert outside.is_dir()
        assert state.legacy_drop_retirement.quarantined == ()
        if with_legacy_line:
            assert (outside / "coding_done.py").read_text() == "THE TARGET'S\n"

    def test_same_basename_worktrees_never_share_a_quarantine(self, tmp_path):
        """Two retirements in one second, same worktree name, distinct bytes."""
        wt = make_git_worktree(tmp_path)
        twin = tmp_path / "elsewhere" / wt.worktree_path.name
        twin.parent.mkdir()
        _git_out(wt.main_repo, "worktree", "add", str(twin), "-b", "twin")
        _plant_legacy_foreign_drop(wt).write_text("FIRST = True\n")
        _plant_legacy_foreign_drop(wt, twin).write_text("SECOND = True\n")

        first = _setup(wt.main_repo).apply(wt.worktree_path).legacy_drop_retirement
        second = _setup(wt.main_repo).apply(twin).legacy_drop_retirement

        assert first.quarantine_dir is not None and second.quarantine_dir is not None
        assert first.quarantine_dir != second.quarantine_dir
        assert (first.quarantine_dir / LEGACY_TOOL).read_text() == "FIRST = True\n"
        assert (second.quarantine_dir / LEGACY_TOOL).read_text() == "SECOND = True\n"

    def test_concurrent_setup_append_survives_the_legacy_line_rewrite(
        self, tmp_path, monkeypatch
    ):
        """Both writers share one ``info/exclude``; neither may lose the other's
        entries. The other setup's append is fired between retirement's read
        and its rewrite of the file."""
        import threading

        from issue_orchestrator.adapters.worktree import _worktree_git_exclude
        from issue_orchestrator.adapters.worktree._worktree_git_exclude import (
            append_worktree_exclude_entries,
        )

        wt = make_git_worktree(tmp_path)
        other = tmp_path / "wt-other"
        _git_out(wt.main_repo, "worktree", "add", str(other), "-b", "other")
        _plant_legacy_foreign_drop(wt)
        real_write = _worktree_git_exclude.atomic_write_bytes
        appended: list[threading.Thread] = []

        def write_after_a_concurrent_append(path, payload):
            thread = threading.Thread(
                target=append_worktree_exclude_entries,
                args=(other, [Path("concurrent/runtime.lock")]),
            )
            thread.start()
            appended.append(thread)
            thread.join(timeout=0.5)  # blocks on the lock when it is held
            real_write(path, payload)

        monkeypatch.setattr(
            _worktree_git_exclude, "atomic_write_bytes", write_after_a_concurrent_append
        )

        state = _setup(wt.main_repo).apply(wt.worktree_path)
        for thread in appended:
            thread.join(timeout=10)

        assert state.legacy_drop_retirement.exclude_lines_removed is True
        common_exclude = (wt.main_repo / ".git" / "info" / "exclude").read_text()
        assert LEGACY_TOOL.as_posix() not in common_exclude
        assert "concurrent/runtime.lock" in common_exclude.splitlines()


class TestEnforcedHooksAreAnInvariantNotARequest:
    """``hooks_installed`` must be an observed outcome, never the input echoed.

    A worktree that reports enforced guardrails while having none is worse than
    one that fails to be created: the session runs and can push past validation.
    """

    def test_enforced_hooks_that_cannot_be_installed_fail_setup(self, tmp_path):
        wt = make_git_worktree(tmp_path)
        missing_hook = tmp_path / "nonexistent-pre-push"

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(
                repo_root=wt.main_repo,
                enforce_hooks=True,
                pre_push_hook=missing_hook,
            ).apply(wt.worktree_path)

        assert not (wt.hooks_dir / "pre-push").exists()

    def test_enforced_hooks_fail_when_git_config_will_not_take(self, tmp_path):
        """The hook file can land in a directory git never consults.

        Nothing about the filesystem looks wrong in this case — the hooks
        directory is writable, the copy succeeds — so an owner that only checks
        for the file reports a guardrail that will never run.
        """
        wt = make_git_worktree(tmp_path)
        block_worktree_config_writes(wt.gitdir)

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(
                repo_root=wt.main_repo, enforce_hooks=True
            ).apply(wt.worktree_path)

        assert effective_hooks_path(wt.worktree_path) != str(wt.hooks_dir)

    def test_enforced_hooks_fail_when_the_worktree_has_no_git_link(
        self, repo_root, tmp_path
    ):
        # No ``.git`` link means hook installation has nowhere to write; the
        # old owner reported success anyway.
        detached = tmp_path / "detached"
        detached.mkdir()

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(repo_root=repo_root, enforce_hooks=True).apply(detached)


class TestOwnerErrorBoundary:
    """``WorktreeError`` is the owner's whole failure surface."""

    def test_step_failures_are_translated_to_worktree_error(
        self, repo_root, worktree_path
    ):
        # A file where git's ``info/`` directory belongs makes the exclude
        # write raise a bare OSError from inside a composed step.
        (repo_root / ".git" / "worktrees" / "repo-123" / "info").write_text(
            "not a directory"
        )

        with pytest.raises(WorktreeError, match="Worktree runtime setup failed"):
            _setup(repo_root).apply(worktree_path)


class TestNoVerifyDryRunFlag:
    """The flag gates a hook bypass, so both directions must actually land."""

    def test_flag_is_written_when_preflight_allows_no_verify(
        self, repo_root, worktree_path
    ):
        _setup(repo_root, allow_no_verify_dry_run_preflight=True).apply(worktree_path)

        assert (worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH).exists()

    def test_stale_flag_is_cleared_when_preflight_disallows_no_verify(
        self, repo_root, worktree_path
    ):
        stale = worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH
        stale.parent.mkdir(parents=True)
        stale.write_text("allow\n")

        _setup(repo_root, allow_no_verify_dry_run_preflight=False).apply(worktree_path)

        assert not stale.exists()

    def test_unwritable_flag_fails_setup_instead_of_leaving_it_ambiguous(
        self, repo_root, worktree_path
    ):
        # A file where the runtime directory belongs makes every write under it
        # fail; setup must surface that rather than run with an unknown flag state.
        (worktree_path / ".issue-orchestrator").write_text("not a directory")

        with pytest.raises(WorktreeError, match="no-verify dry-run flag"):
            _setup(repo_root, allow_no_verify_dry_run_preflight=True).apply(
                worktree_path
            )


class TestWorktreeIdentityFailureSemantics:
    """A worktree identity nobody can read back is worse than no worktree."""

    def test_unpersistable_identity_fails_setup(self, repo_root, worktree_path):
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.mkdir()  # a directory where the marker file belongs

        with pytest.raises(WorktreeError, match="worktree identity"):
            _setup(repo_root).apply(worktree_path)

    def test_empty_identity_marker_is_regenerated(self, repo_root, worktree_path):
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("   \n")

        state = _setup(repo_root).apply(worktree_path)

        assert state.worktree_id.startswith("wt-")
        assert marker.read_text() == state.worktree_id

    def test_non_utf8_identity_marker_is_regenerated(self, repo_root, worktree_path):
        # Undecodable bytes carry no identity, so replacing them loses nothing.
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"\xff\xfe not an id")

        state = _setup(repo_root).apply(worktree_path)

        assert state.worktree_id.startswith("wt-")
        assert marker.read_text() == state.worktree_id

    def test_unreadable_identity_marker_fails_without_reissuing_the_identity(
        self, repo_root, worktree_path, monkeypatch
    ):
        # A read that fails is not evidence the identity is gone. Regenerating
        # would tell every job holding "wt-original" that its worktree was
        # replaced underneath it.
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("wt-original")
        _break_read_of(monkeypatch, marker, PermissionError("simulated read failure"))

        with pytest.raises(WorktreeError, match="read worktree identity marker"):
            _setup(repo_root).apply(worktree_path)

        assert marker.read_bytes() == b"wt-original"


class TestInstallClaudeSettingsFailureSemantics:
    """The Stop hook is a completion guardrail; installing it cannot half-fail."""

    def _stop_hook_commands(self, settings_file: Path) -> list[str]:
        settings = json.loads(settings_file.read_text())
        return [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]

    def test_corrupt_settings_are_replaced_with_the_enforced_hook(
        self, tmp_path, caplog
    ):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text("{not json")

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "Replacing unreadable Claude settings" in caplog.text

    def test_wrong_shaped_hooks_are_replaced_rather_than_crashing(
        self, tmp_path, caplog
    ):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"hooks": ["not-an-object"]}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-object 'hooks'" in caplog.text

    def test_null_hooks_are_replaced_rather_than_crashing(self, tmp_path, caplog):
        # Regression: an explicit JSON ``null`` used to be indistinguishable
        # from a missing key, so the merge tried to ``setdefault`` into None
        # and raised AttributeError out of worktree setup.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"model": "opus", "hooks": None}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-object 'hooks'" in caplog.text

    def test_missing_hooks_key_preserves_operator_settings(self, tmp_path):
        # The counterpart to the null case: absent really is absent, and an
        # operator's unrelated settings must survive the merge.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}})
        )

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())
        assert settings["model"] == "opus"
        assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
        assert self._stop_hook_commands(settings_file) == [
            CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"][0]["hooks"][0]["command"]
        ]

    def test_non_utf8_settings_are_replaced(self, tmp_path, caplog):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_bytes(b"\xff\xfe{")

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-UTF-8 Claude settings" in caplog.text

    def test_unreadable_settings_fail_without_discarding_operator_content(
        self, tmp_path, monkeypatch
    ):
        # Failing to read a file says nothing about what is in it. Overwriting
        # on a read error silently deletes operator settings that were fine.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        original = json.dumps({"model": "opus"})
        settings_file.write_text(original)
        _break_read_of(
            monkeypatch, settings_file, PermissionError("simulated read failure")
        )

        with pytest.raises(WorktreeError, match="read existing Claude settings"):
            install_claude_settings(tmp_path)

        assert settings_file.read_bytes() == original.encode()

    def test_non_list_stop_hooks_are_replaced(self, tmp_path, caplog):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"hooks": {"Stop": "nope"}}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-list 'hooks.Stop'" in caplog.text

    def test_repeated_installs_do_not_duplicate_the_stop_hook(self, tmp_path):
        install_claude_settings(tmp_path)
        install_claude_settings(tmp_path)

        commands = self._stop_hook_commands(tmp_path / ".claude" / "settings.json")
        assert len(commands) == 1

    def test_existing_operator_settings_survive_the_merge(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]
                    },
                }
            )
        )

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())
        assert settings["model"] == "opus"
        assert "echo mine" in self._stop_hook_commands(settings_file)
        assert len(settings["hooks"]["Stop"]) == 2

    def test_install_does_not_mutate_the_shared_settings_template(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "echo mine"}]}]}})
        )

        install_claude_settings(tmp_path)

        assert len(CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"]) == 1

    def test_unwritable_settings_fail_setup(self, tmp_path):
        (tmp_path / ".claude").write_text("not a directory")

        with pytest.raises(WorktreeError, match="Claude settings"):
            install_claude_settings(tmp_path)
