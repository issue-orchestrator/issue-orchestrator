"""A checkout in custody survives every removal path there is (#7274).

A tech-lead failure investigation runs in a disposable worktree on a branch that
is never pushed, so from the moment its session ends, the branch exists in
exactly one place: on disk, in that checkout. Four paths remove it and three ask
for FORCED removal, which falls back to ``shutil.rmtree`` when git refuses --
``git worktree lock`` is metadata that ``git worktree prune`` honours and a
filesystem delete does not.

These tests run against real repositories and real worktrees, because the claim
worth making is not "a flag is checked" but "the directory and the branch are
still there afterwards".
"""

from __future__ import annotations

import ast
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from issue_orchestrator.adapters.worktree.custody import (
    CUSTODY_DIR,
    CUSTODY_FILE,
    CUSTODY_LOCK,
    CUSTODY_LOG,
    GitMetadataWorktreeCustody,
    custody_guard,
    custody_prune_guard,
    git_common_dir,
)
import issue_orchestrator.adapters.worktree._worktree as worktree_module
from issue_orchestrator.adapters.budgeted_validation_git import (
    BudgetedValidationGit,
)
from issue_orchestrator.control.maintenance import _remove_local_worktree
from tests.e2e.fixtures.cleanup import cleanup_local_worktrees
from issue_orchestrator.ports.worktree_custody import (
    CustodyUnavailableError,
    WorktreeCustody,
)
from issue_orchestrator.control.worktree_reconciliation import (
    WorktreeActivityEvidence,
    WorktreeAuditOwner,
)
from issue_orchestrator.entrypoints.cli_tools.worktree_custody import (
    main as custody_cli,
)
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
from issue_orchestrator.ports.worktree_custody import (
    CustodyError,
    CustodyRelease,
    WorktreeInCustodyError,
)
import issue_orchestrator.adapters.worktree.removal as removal_module
from issue_orchestrator.adapters.worktree.removal import remove_checkout_path
from issue_orchestrator.adapters.worktree.worktree_policy import (
    ValidateOrDeletePolicy,
)
from issue_orchestrator.execution.reviewer_worktree import (
    ReviewerWorktree,
    remove_reviewer_worktree,
)
from issue_orchestrator.ports.worktree_manager import (
    WORKTREE_ID_MARKER,
    WorktreeReuseOptions,
)

HOLDER = "operator"
REASON = "investigation #6410 left commits only on this branch"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _git_in(repo: Path):
    """A git runner for the removal owner, reporting failure as text."""

    def run(argv: list[str]) -> "str | None":
        result = subprocess.run(
            ["git", "-C", str(repo), *argv], capture_output=True, text=True
        )
        return None if result.returncode == 0 else (result.stderr or "").strip()

    return run


@contextmanager
def _unreadable(directory: Path) -> "Iterator[None]":
    """Make everything under ``directory`` genuinely unstattable.

    Monkeypatching ``Path.stat``/``Path.lstat`` does NOT work for this on
    Python 3.14: ``Path.exists()`` calls ``os.stat`` directly, so a patched
    ``Path.stat`` leaves ``exists()`` answering True and the mutation under test
    becomes inert -- a test that passes with the fix reverted. Removing the
    parent's execute bit makes the kernel raise EACCES for real, which is the
    condition the production code actually has to survive.
    """
    original = directory.stat().st_mode
    os.chmod(directory, 0o000)
    try:
        yield
    finally:
        os.chmod(directory, original)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit on ``main``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--initial-branch=main", ".")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")
    return root


#: The scratch run token the fixtures below share, so a worktree basename and
#: its branch name agree the way ``names_one_scratch_checkout`` requires.
TOKEN = "abcdef123456"


@pytest.fixture
def checkout(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree on its own branch, with a commit only it has."""
    path = tmp_path / "worktree" / "repo-tech-lead-6410-abcdef123456"
    _git(repo, "worktree", "add", "-b", "tech-lead-investigation-6410-abcdef123456", str(path))
    (path / "finding.md").write_text("the thing I found\n")
    _git(path, "add", "finding.md")
    _git(path, "commit", "-m", "the only copy of this work")
    return path


@pytest.fixture
def manager(repo: Path) -> GitWorktreeManager:
    """Bound to its repository, the way the composition root builds it.

    Required, not convenient: custody lives in the REPOSITORY's metadata, and a
    manager that does not know which repository it serves cannot answer for a
    checkout that has lost its own ``.git`` file.
    """
    return GitWorktreeManager(repo)


def _mark_orchestrator_owned(checkout: Path) -> None:
    """The identity marker reconciliation requires before it will classify."""
    marker = checkout / WORKTREE_ID_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("wt-test\n")


def _branches(repo: Path) -> set[str]:
    return {
        line.strip()
        for line in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        if line.strip()
    }


def _load_teardown_test_issues() -> Any:
    """The SHIPPED teardown script, loaded as a module.

    Driven directly because it removes no directory, so nothing in the removal
    seam or its static guard sees it (round 23 finding 1).
    """
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "teardown_test_issues.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_teardown_test_issues_for_custody", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEveryRemovalPathAsks:
    """Enforcement is at the removal seam, so a new caller cannot bypass it."""

    @pytest.mark.parametrize("force", [False, True], ids=["plain", "forced"])
    def test_a_held_checkout_is_refused(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, force: bool
    ) -> None:
        """``force`` means "try harder with git", never "custody does not apply"."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError) as caught:
            manager.remove_checkout(checkout, force=force)

        assert caught.value.grant.holder == HOLDER
        assert (checkout / "finding.md").exists()

    @pytest.mark.parametrize("force", [False, True], ids=["plain", "forced"])
    def test_a_held_checkouts_branch_is_never_deleted(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, force: bool
    ) -> None:
        """The path that deletes the branch is where a held checkout costs most."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            manager.remove_checkout_and_branch(checkout, force=force)

        assert "tech-lead-investigation-6410-abcdef123456" in _branches(repo)
        assert (checkout / "finding.md").exists()

    def test_an_unheld_checkout_is_removed_as_before(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Custody changes nothing for the checkouts nobody claimed."""
        manager.remove_checkout_and_branch(checkout, force=True)

        assert not checkout.exists()
        assert "tech-lead-investigation-6410-abcdef123456" not in _branches(repo)


class TestReleasing:
    def test_an_explicit_release_lets_the_removal_through(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        manager.remove_checkout_and_branch(
            checkout,
            force=True,
            custody_release=CustodyRelease(holder="operator", reason="collected"),
        )

        assert not checkout.exists()
        assert manager.custody_of(checkout) is None

    def test_releasing_without_removing_leaves_the_checkout(
        self, manager: GitWorktreeManager, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        released = manager.release_custody(
            checkout, CustodyRelease(holder=HOLDER, reason="collected")
        )

        assert released is not None and released.holder == HOLDER
        assert manager.custody_of(checkout) is None
        assert (checkout / "finding.md").exists()

    def test_releasing_something_nobody_holds_says_so(
        self, manager: GitWorktreeManager, checkout: Path
    ) -> None:
        assert (
            manager.release_custody(
                checkout, CustodyRelease(holder=HOLDER, reason="collected")
            )
            is None
        )

    def test_a_release_needs_a_holder_and_a_reason(self) -> None:
        """An audit trail of empty strings answers nothing later."""
        for holder, reason in (("", "collected"), (HOLDER, "  ")):
            with pytest.raises(ValueError, match="custody release requires"):
                CustodyRelease(holder=holder, reason=reason)


class TestTheGrant:
    def test_taking_a_checkout_someone_else_holds_returns_their_grant(
        self, manager: GitWorktreeManager, checkout: Path
    ) -> None:
        """The first holder keeps it; the second caller learns who has it."""
        first = manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        second = manager.take_custody(
            checkout, holder="someone-else", reason="I want it"
        )

        assert second == first

    def test_a_grant_records_the_branch_it_protects(
        self, manager: GitWorktreeManager, checkout: Path
    ) -> None:
        """So a refusal can name the branch, not just the directory."""
        grant = manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        assert grant.branch == "tech-lead-investigation-6410-abcdef123456"
        assert grant.reason == REASON

    def test_a_relative_path_finds_the_same_grant(
        self, manager: GitWorktreeManager, checkout: Path, monkeypatch
    ) -> None:
        """One spelling per checkout, or a grant hides behind a `..`."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        monkeypatch.chdir(checkout.parent)

        assert manager.custody_of(Path(checkout.name)) is not None

    def test_an_unregistered_path_cannot_be_held(
        self, manager: GitWorktreeManager, tmp_path: Path
    ) -> None:  # noqa: D102
        """Failing beats reporting a path nobody can protect as protected."""
        orphan = tmp_path / "not-a-repo"
        orphan.mkdir()

        with pytest.raises(CustodyUnavailableError, match="protect nothing"):
            manager.take_custody(orphan, holder=HOLDER, reason=REASON)


class TestTheStore:
    def test_every_worktree_of_a_repository_shares_one_store(
        self, repo: Path, checkout: Path
    ) -> None:
        """A per-worktree store is one every other removal path would miss."""
        assert git_common_dir(checkout) == git_common_dir(repo) == repo / ".git"

    def test_custody_survives_a_restart(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Nothing is cached in the process that took it."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        held = GitWorktreeManager(repo).custody_of(checkout)

        assert held is not None and held.holder == HOLDER
        assert (repo / ".git" / CUSTODY_FILE).exists()

    def test_the_record_outlives_the_checkout_it_protected(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Kept beside the repository, not inside the thing being deleted."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        manager.remove_checkout_and_branch(
            checkout,
            force=True,
            custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
        )

        trail = [
            json.loads(line)
            for line in (repo / ".git" / CUSTODY_LOG).read_text().splitlines()
        ]

        assert [entry["action"] for entry in trail] == [
            "take",
            # The INTENT is on record before the removal runs, so a process
            # that dies mid-removal leaves an audited hand-off rather than a
            # breach nobody asked for.
            "release-intent",
            "release",
        ]
        assert trail[-1]["reason"] == "collected"
        assert trail[-1]["branch"] == "tech-lead-investigation-6410-abcdef123456"

    def test_an_unreadable_held_checkout_is_not_reported_as_gone(
        self, manager: GitWorktreeManager, repo: Path, tmp_path: Path
    ) -> None:
        """Inspection failure is unknown, not evidence of an external deletion.

        ``breached()`` is what tells the operator their protected branch was
        removed anyway. Built on ``Path.exists()``, it said that about a
        checkout it merely could not read (round 18 finding 1).
        """
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        held = sealed / "repo-tech-lead-6410-abcdef123456"
        _git(repo, "worktree", "add", "-b", "sealed-investigation", str(held))
        (held / "finding.md").write_text("the only copy\n")
        grant = manager.take_custody(held, holder=HOLDER, reason=REASON)

        with _unreadable(sealed):
            # The premise, asserted rather than assumed: this is exactly the
            # state in which exists() answers False about a path that is there.
            assert held.exists() is False

            with pytest.raises(
                CustodyUnavailableError,
                match="cannot verify whether held checkout",
            ):
                manager.breached_custody(repo)

        assert manager.custody_of(held) == grant
        assert (held / "finding.md").exists()

    def test_removal_does_not_mistake_an_unreadable_held_checkout_for_absent(
        self, manager: GitWorktreeManager, repo: Path, tmp_path: Path
    ) -> None:
        """The manager has to REACH custody before declaring success.

        ``remove_worktree`` took an early exit on ``Path.exists()``, so a forced
        removal of an unreadable held checkout returned NORMALLY -- reporting
        success for a checkout it never touched and never asked about (round 19
        finding 2).
        """
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        held = sealed / "repo-tech-lead-6410-abcdef123456"
        _git(repo, "worktree", "add", "-b", "sealed-investigation", str(held))
        (held / "finding.md").write_text("the only copy\n")
        grant = manager.take_custody(held, holder=HOLDER, reason=REASON)

        with _unreadable(sealed):
            assert held.exists() is False

            with pytest.raises(CustodyError):
                manager.remove_checkout_and_branch(held, force=True)

        assert manager.custody_of(held) == grant
        assert (held / "finding.md").exists()

    def test_an_unreadable_store_is_an_error_not_an_empty_one(
        self, repo: Path, checkout: Path
    ) -> None:
        """"Nothing is held" is the one answer a broken store must not give."""
        store = repo / ".git" / CUSTODY_FILE
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{ not json")

        with pytest.raises(CustodyUnavailableError, match="unreadable"):
            GitMetadataWorktreeCustody.for_path(checkout).held(checkout)  # type: ignore[union-attr]

    def test_a_damaged_store_stops_a_removal_instead_of_failing_open(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """An unreadable store is not permission (round 1 finding 4).

        Reuse cleanup deletes the directory on any exception from git, so a
        custody store it cannot parse must not arrive there looking like an
        ordinary removal failure.
        """
        store = repo / ".git" / CUSTODY_FILE
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{ not json")

        for removal in (
            lambda: manager.remove_checkout_and_branch(checkout, force=True),
            lambda: ValidateOrDeletePolicy().delete_worktree(checkout, repo),
        ):
            with pytest.raises(CustodyUnavailableError):
                removal()

        assert (checkout / "finding.md").exists()

    def test_a_lock_failure_is_typed_as_custody_unavailable(
        self, manager: GitWorktreeManager, checkout: Path, monkeypatch
    ) -> None:
        """A filesystem that cannot lock leaves custody UNKNOWN.

        The raw OSError was not a CustodyError, so it escaped the typed
        propagation and termination reported the checkout as an unprotected
        leak (round 16 finding 1).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        def lock_fails(*_args: object) -> None:
            raise OSError("locking is unavailable")

        monkeypatch.setattr(fcntl, "flock", lock_fails)

        with pytest.raises(CustodyUnavailableError, match="cannot lock"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_an_unreadable_git_pointer_stops_a_removal(
        self, manager: GitWorktreeManager, checkout: Path
    ) -> None:
        """Reporting "not in a repository" there would report "not held"."""
        (checkout / ".git").write_text("this is not a gitdir pointer\n")

        with pytest.raises(CustodyUnavailableError):
            manager.remove_checkout(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_relative_gitdir_pointer_finds_the_same_store(
        self, repo: Path, checkout: Path, monkeypatch
    ) -> None:
        """git writes these; resolving one against the CWD finds nothing."""
        absolute = git_common_dir(checkout)
        pointer = checkout / ".git"
        target = Path(pointer.read_text().split(":", 1)[1].strip())
        pointer.write_text(f"gitdir: {os.path.relpath(target, checkout)}\n")
        monkeypatch.chdir(checkout.parent)

        assert git_common_dir(checkout) == absolute

    def test_held_checkouts_are_listed_oldest_first(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        second = tmp_path / "worktree" / "repo-tech-lead-6411-abcdef123456"
        _git(repo, "worktree", "add", "-b", "tech-lead-investigation-6411-abcdef123456", str(second))
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        manager.take_custody(second, holder="someone-else", reason="also mine")

        held = manager.checkouts_in_custody(repo)

        assert [grant.path for grant in held] == [checkout, second]


class TestTheOperatorSurface:
    """A person can hold a checkout without a working orchestrator config.

    Custody lives in the repository's own git metadata precisely so it answers
    for a clone whose configuration is missing, which is the state a salvage
    tends to happen in.
    """

    def test_holding_a_checkout_protects_it_from_removal(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        exit_code = custody_cli(
            ["hold", str(checkout), "--reason", REASON, "--holder", HOLDER]
        )

        assert exit_code == 0
        with pytest.raises(WorktreeInCustodyError):
            manager.remove_checkout_and_branch(checkout, force=True)

    def test_holding_one_someone_else_has_fails_and_names_them(
        self, manager: GitWorktreeManager, checkout: Path, capsys
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        exit_code = custody_cli(
            ["hold", str(checkout), "--reason", "mine now", "--holder", "someone-else"]
        )

        assert exit_code == 1
        assert HOLDER in capsys.readouterr().err

    def test_releasing_then_removing_works(
        self, manager: GitWorktreeManager, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        assert (
            custody_cli(
                ["release", str(checkout), "--reason", "collected", "--holder", HOLDER]
            )
            == 0
        )

        manager.remove_checkout_and_branch(checkout, force=True)
        assert not checkout.exists()

    def test_releasing_one_nobody_holds_fails(self, checkout: Path) -> None:
        assert (
            custody_cli(
                ["release", str(checkout), "--reason", "collected", "--holder", HOLDER]
            )
            == 1
        )

    def test_listing_names_the_branch_at_risk(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, capsys
    ) -> None:
        """The operator needs the branch name, not just a directory."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        assert custody_cli(["list", "--repo-root", str(repo)]) == 0

        printed = capsys.readouterr().out
        assert "tech-lead-investigation-6410-abcdef123456" in printed
        assert REASON in printed

    def test_listing_continues_after_an_unreadable_checkout(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        checkout: Path,
        tmp_path: Path,
        capsys,
    ) -> None:
        """One unknown checkout does not hide valid grants or later breaches.

        ``cmd_list`` asked for the breach list before printing anything, so a
        raise there aborted the whole command (round 19 finding 1).
        """
        manager.take_custody(checkout, holder="present-holder", reason="still here")

        gone = checkout.parent / "gone-checkout"
        _git(repo, "worktree", "add", "-b", "gone-branch", str(gone))
        manager.take_custody(gone, holder="gone-holder", reason="was here")
        shutil.rmtree(gone)

        sealed = tmp_path / "sealed"
        sealed.mkdir()
        unknown = sealed / "repo-tech-lead-6410-abcdef123456"
        _git(repo, "worktree", "add", "-b", "sealed-branch", str(unknown))
        manager.take_custody(unknown, holder="sealed-holder", reason="cannot see")

        with _unreadable(sealed):
            assert custody_cli(["list", "--repo-root", str(repo)]) == 1
            captured = capsys.readouterr()

        assert "present-holder" in captured.out
        assert "GONE despite being held" in captured.out
        assert "gone-branch" in captured.out
        assert "UNKNOWN -- checkout presence could not be verified" in captured.out
        assert "sealed-holder" in captured.out
        assert "Permission denied" in captured.out

    def test_listing_an_empty_repository_says_so(
        self, repo: Path, capsys
    ) -> None:
        assert custody_cli(["list", "--repo-root", str(repo)]) == 0

        assert "No checkouts are in custody." in capsys.readouterr().out

    def test_a_hold_without_a_reason_is_refused(self, checkout: Path) -> None:
        """The reason is the thing the audit trail exists to carry."""
        with pytest.raises(SystemExit):
            custody_cli(["hold", str(checkout)])


class TestReconciliationSeesCustody:
    """Startup says why a held checkout stays, instead of failing to remove it."""

    def _audit(self, repo: Path, base: Path) -> tuple:
        return WorktreeAuditOwner(GitWorktreeManager(repo)).audit(
            repo_root=repo,
            worktree_base=base,
            activity=WorktreeActivityEvidence.known(set()),
        )

    def test_a_held_scratch_checkout_is_retained_and_says_who_holds_it(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        _mark_orchestrator_owned(checkout)
        before = self._audit(repo, checkout.parent)
        assert [entry.disposition for entry in before] == ["cleanup_candidate"]

        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        after = self._audit(repo, checkout.parent)
        assert [entry.disposition for entry in after] == ["retained"]
        assert after[0].reason == f"in the custody of {HOLDER}"


def _is_call(node: ast.AST, name: str) -> bool:
    """A CALL of ``name``: an import left behind proves nothing."""
    return (
        isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == name
    )


def _removal_lines(tree: ast.AST) -> list[int]:
    """Every line spelling ``worktree`` then ``remove`` as adjacent arguments.

    Both shapes count: a list literal passed as one argument, and a vararg call
    spreading the words across positional arguments.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        groups: list[tuple[int, list[ast.expr]]] = []
        if isinstance(node, (ast.List, ast.Tuple)):
            groups.append((node.lineno, list(node.elts)))
        elif isinstance(node, ast.Call):
            groups.append((node.lineno, list(node.args)))
        for lineno, elements in groups:
            words = [
                element.value
                for element in elements
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            for first, second in zip(words, words[1:]):
                if first == "worktree" and second == "remove":
                    lines.add(lineno)
    return sorted(lines)



def _branch_deletion_nodes(tree: ast.AST) -> list[ast.AST]:
    """Every literal local ``git branch -d/-D`` command builder."""
    found: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        groups: list[list[ast.expr]] = []
        if isinstance(node, (ast.List, ast.Tuple)):
            groups.append(list(node.elts))
        elif isinstance(node, ast.Call):
            groups.append(list(node.args))
        for elements in groups:
            words = [
                element.value
                for element in elements
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            if any(
                first == "branch" and second in {"-d", "-D", "--delete"}
                for first, second in zip(words, words[1:])
            ):
                found[id(node)] = node
    return list(found.values())


def _runtime_nodes(root: ast.AST) -> list[ast.AST]:
    """Nodes executed while this lexical block is ACTIVE.

    A function or lambda DEFINED inside a custody guard may be called after the
    guard exits, so its body is not protected merely because its AST is nested
    beneath the ``with``. Walking into those bodies would let a refactor move
    the deletion into a callable invoked afterwards and keep this guard green
    -- the round-16 vacuity in a new shape (round 17 finding 2).

    Definition-time expressions are the other half of the same question, and
    excluding a whole node was too blunt for them: decorators and defaults run
    immediately, as does a class body. A generator expression splits the two --
    its OUTERMOST iterable is evaluated at construction, while its element,
    filters and remaining iterators run only when it is advanced, so a deletion
    placed in one and consumed after the guard was still counted as guarded
    (round 18 finding 2).
    """
    found: list[ast.AST] = []
    pending = [root]
    while pending:
        node = pending.pop()
        found.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            children = [
                *node.decorator_list,
                *node.args.defaults,
                *(d for d in node.args.kw_defaults if d is not None),
            ]
        elif isinstance(node, ast.Lambda):
            children = [
                *node.args.defaults,
                *(d for d in node.args.kw_defaults if d is not None),
            ]
        elif isinstance(node, ast.GeneratorExp):
            children = [node.generators[0].iter]
        else:
            # Class bodies and list/set/dict comprehensions execute eagerly.
            children = list(ast.iter_child_nodes(node))
        pending.extend(reversed(children))
    return found


def _custody_lock_is_held(repo: Path) -> bool:
    """Whether the custody lock is taken, asked the way another process would.

    ``flock`` is per open file DESCRIPTION, so a descriptor this helper opens is
    refused exactly when a descriptor in another process would be -- no thread,
    no sleep, and no window to lose.
    """
    path = git_common_dir(repo) / CUSTODY_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


class TestOneRemovalOwner:
    """``git worktree remove`` is built in exactly one module.

    The shape of #7274 was not a missing check. It was NINE places that removed
    a worktree -- four lifecycle paths, a reuse-cleanup fallback, a reviewer
    rollback, a publication workspace, a validation lane, an E2E fixture and a
    doctor repair -- each with its own command and most with their own
    ``shutil.rmtree`` for when git declined. Custody added to nine places is
    custody missing from the tenth.

    So the rule is ownership, not inspection: everything calls
    ``removal.remove_checkout_path``, which asks custody once and holds the
    answer across both attempts. This is the same guardrail shape the
    repository already uses for the process table and the provider-output
    classifier -- one owner, named, and a test that says so.
    """

    OWNER = "src/issue_orchestrator/adapters/worktree/removal.py"

    #: Every directory of SHIPPED code. Each round of review found the command
    #: somewhere the scan did not read -- ``scripts`` (round 3),
    #: ``repo-specific`` (round 4) -- so it reads all of them.
    #:
    #: Ordinary tests are excluded: a test building a worktree fixture is not
    #: the orchestrator removing somebody's checkout. ``tests/e2e/fixtures`` IS
    #: included, because its cleanup helper sweeps real operator-visible
    #: worktrees -- it used to be a second removal owner (round 7 finding 1),
    #: so excluding it would let that exact regression satisfy this guard again
    #: (round 10 finding 2).
    #:
    #: What this does NOT do is hunt ``shutil.rmtree``. Any line anywhere can
    #: delete a directory, and a guardrail chasing that is a search with no end
    #: -- four review rounds each found one more. The owner PREVENTS what goes
    #: through it; ``GitMetadataWorktreeCustody.breached`` DETECTS what does not.
    SEARCHED = ("src", "scripts", "tools", "repo-specific", "tests/e2e/fixtures")

    def _builders(self) -> dict[str, list[int]]:
        root = Path(__file__).resolve().parents[2]
        found: dict[str, list[int]] = {}
        for directory in self.SEARCHED:
            for path in sorted((root / directory).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                lines = _removal_lines(tree)
                if lines:
                    found[str(path.relative_to(root))] = lines
        return found

    def test_the_real_e2e_cleanup_is_inside_the_scan(self) -> None:
        """The operational cleanup helper is not an ordinary test fixture.

        Pinned separately from the scan itself: a future tidy-up that drops
        ``tests/e2e/fixtures`` from SEARCHED would silently restore the blind
        spot rather than fail anything.
        """
        root = Path(__file__).resolve().parents[2]
        scanned = {
            path.resolve()
            for directory in self.SEARCHED
            for path in (root / directory).rglob("*.py")
        }

        assert (root / "tests/e2e/fixtures/cleanup.py").resolve() in scanned

    def test_only_the_owner_builds_the_removal_command(self) -> None:
        builders = self._builders()

        assert self.OWNER in builders, (
            f"{self.OWNER} no longer builds the removal command; this guard is "
            "pointing at the wrong owner"
        )
        assert set(builders) == {self.OWNER}, (
            "these modules build their own `git worktree remove`: "
            f"{sorted(set(builders) - {self.OWNER})}. Call "
            "removal.remove_checkout_path instead -- it asks custody once and "
            "holds the answer across the git attempt AND the filesystem "
            "fallback."
        )

    #: The two things the owner does that destroy a checkout. Both must sit
    #: inside its guard -- a guard around only the git attempt leaves the
    #: filesystem fallback in the window an operator can take custody in.
    DESTRUCTIVE = ("_remove_with_git", "_delete_path", "rmdir")

    def test_both_destructive_steps_run_inside_the_guard(self) -> None:
        """Ancestry, not presence.

        "Some ``custody_guard`` call exists in this module" was satisfiable by
        dead code, or by a guard wrapping only half the removal (round 6
        finding 5). This is one module, so checking the actual nesting is
        cheap -- which is why declining it would have been laziness rather than
        the boundary argument I made for the repo-wide scan.
        """
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / self.OWNER).read_text(encoding="utf-8"))
        expected_owners = {
            "remove_checkout_path",
            "remove_empty_worktree_container",
        }
        owners = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in expected_owners
        ]
        assert {node.name for node in owners} == expected_owners, (
            "the removal owner no longer exposes the guarded checkout and "
            "container removal operations"
        )
        guarded = [
            node
            for owner in owners
            for node in ast.walk(owner)
            if isinstance(node, ast.With)
            and any(
                _is_call(item.context_expr, "custody_guard") for item in node.items
            )
        ]
        assert guarded, "the removal owner does not ask custody at all"

        # Per CALL SITE, not per NAME. Comparing sets of names meant one
        # guarded decoy call satisfied the assertion while a second live call
        # ran outside the guard (round 16 finding 3).
        destructive_calls = [
            (node, name)
            for node in ast.walk(tree)
            for name in self.DESTRUCTIVE
            if _is_call(node, name)
        ]
        guarded_call_ids = {
            id(node)
            for block in guarded
            for node in _runtime_nodes(block)
            if isinstance(node, ast.Call)
        }
        called = {name for _, name in destructive_calls}
        outside = [
            (name, node.lineno)
            for node, name in destructive_calls
            if id(node) not in guarded_call_ids
        ]

        assert called == set(self.DESTRUCTIVE), (
            f"the owner no longer performs {set(self.DESTRUCTIVE) - called}; "
            "this guard is watching the wrong names"
        )
        assert outside == [], (
            f"these destructive calls run OUTSIDE the custody guard: {outside}"
        )

    def test_every_local_branch_deletion_runs_inside_branch_custody(self) -> None:
        """Branch deletion gets the same non-vacuous guard checkout removal has.

        Custody protects BRANCHES now, and a `branch -D` removes no directory --
        so neither the removal seam nor the removal guard can see it (round 23
        finding 1, round 24 finding 1).
        """
        root = Path(__file__).resolve().parents[2]
        found: dict[str, list[int]] = {}
        outside: list[str] = []
        for directory in self.SEARCHED:
            for path in sorted((root / directory).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                deletions = _branch_deletion_nodes(tree)
                if not deletions:
                    continue
                relative = str(path.relative_to(root))
                found[relative] = sorted(node.lineno for node in deletions)
                guarded_blocks = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.With)
                    and any(
                        _is_call(item.context_expr, "custody_branch_guard")
                        for item in node.items
                    )
                ]
                guarded_ids = {
                    id(node)
                    for block in guarded_blocks
                    for node in _runtime_nodes(block)
                }
                outside.extend(
                    f"{relative}:{node.lineno}"
                    for node in deletions
                    if id(node) not in guarded_ids
                )

        expected = {
            "scripts/teardown_test_issues.py",
            "src/issue_orchestrator/adapters/worktree/_worktree.py",
            "src/issue_orchestrator/infra/doctor/checks/guardrails.py",
        }
        assert expected <= set(found), (
            "the branch-deletion guard became vacuous; expected shipped "
            f"deletion sites are missing: {sorted(expected - set(found))}"
        )
        assert outside == [], (
            "these local branch deletions run outside custody_branch_guard: "
            f"{outside}"
        )

    def test_the_scan_reads_both_ways_a_command_is_built(self) -> None:
        """A list literal and a vararg call are the same removal."""
        as_list = ast.parse('git.run(repo, ["worktree", "remove", str(p)])')
        as_varargs = ast.parse('self.git("worktree", "remove", "--force", str(p))')

        assert _removal_lines(as_list) == [1]
        assert _removal_lines(as_varargs) == [1]

    def test_a_deferred_body_is_not_mistaken_for_guarded_execution(self) -> None:
        """Nesting under the ``with`` is not the same fact as running in it."""
        tree = ast.parse(
            """
def remove_checkout_path(path):
    with custody_guard(path):
        remove_later = lambda: _delete_path(path)
    remove_later()
"""
        )
        owner = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef)
        )
        guarded = next(
            node for node in ast.walk(owner) if isinstance(node, ast.With)
        )
        destructive = next(
            node for node in ast.walk(owner) if _is_call(node, "_delete_path")
        )

        guarded_call_ids = {
            id(node)
            for node in _runtime_nodes(guarded)
            if isinstance(node, ast.Call)
        }

        assert id(destructive) not in guarded_call_ids

    def test_a_generator_body_is_not_mistaken_for_guarded_execution(self) -> None:
        """Constructing a generator does not execute its element expression."""
        tree = ast.parse(
            """
def remove_checkout_path(path):
    with custody_guard(path):
        remove_later = (_delete_path(path) for _ in (None,))
    next(remove_later)
"""
        )
        owner = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef)
        )
        guarded = next(
            node for node in ast.walk(owner) if isinstance(node, ast.With)
        )
        destructive = next(
            node for node in ast.walk(owner) if _is_call(node, "_delete_path")
        )

        guarded_call_ids = {
            id(node)
            for node in _runtime_nodes(guarded)
            if isinstance(node, ast.Call)
        }

        assert id(destructive) not in guarded_call_ids

    def test_definition_time_calls_are_still_recognised_as_guarded(self) -> None:
        """The other direction: do not reject calls that DO run in the guard."""
        tree = ast.parse(
            """
def remove_checkout_path(path):
    with custody_guard(path):
        @decorate(_delete_path(path))
        def remove_later(value=_delete_path(path)):
            pass

        remove_lambda = lambda value=_delete_path(path): None
        eager_iterable = (value for value in _delete_path(path))

        class Immediate:
            value = _delete_path(path)
"""
        )
        owner = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef)
        )
        guarded = next(
            node for node in ast.walk(owner) if isinstance(node, ast.With)
        )
        destructive = [
            node for node in ast.walk(owner) if _is_call(node, "_delete_path")
        ]

        guarded_call_ids = {
            id(node)
            for node in _runtime_nodes(guarded)
            if isinstance(node, ast.Call)
        }

        assert len(destructive) == 5
        assert {id(node) for node in destructive} <= guarded_call_ids


class TestEachEntryPointRefuses:
    """Driven through the functions production calls, not through the owner.

    A test that enters ``custody_guard`` itself stays green when the caller
    stops routing through the owner, which is how round 3's finding [4] slipped
    past an earlier version of these.
    """

    def test_reviewer_cleanup_refuses_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """It runs ``git worktree remove`` itself, so it asks itself."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        reviewer = ReviewerWorktree(path=checkout, coder_branch="6410-work")

        with pytest.raises(WorktreeInCustodyError):
            remove_reviewer_worktree(reviewer, force=True)

        assert (checkout / "finding.md").exists()


class TestTheRaceBetweenLookingAndDeleting:
    """Checking and then deleting leaves a window custody cannot survive.

    Without the lock spanning the removal, an operator takes custody after the
    remover looked, is told no removal path will discard the checkout, and
    watches it go anyway (round 1 finding 3).
    """

    def test_a_take_uses_the_repository_custody_lock(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        checkout: Path,
        monkeypatch,
    ) -> None:
        """Taking custody and removal serialize on the same lock INODE.

        This was a thread racing a 0.5s timeout: if CI delayed the competing
        thread past the guard, the assertion still held and the test passed
        under the very regression it claims to catch (round 26 finding 1). A
        timeout that IS the mechanism is a race with odds.
        """
        observed_locks: list[tuple[int, int]] = []
        real_flock = fcntl.flock

        def recording_flock(fd: int, operation: int) -> None:
            if operation & fcntl.LOCK_EX:
                opened = os.fstat(fd)
                observed_locks.append((opened.st_dev, opened.st_ino))
            real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", recording_flock)

        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        common = git_common_dir(repo)
        assert common is not None
        lock = (common / CUSTODY_LOCK).stat()
        assert (lock.st_dev, lock.st_ino) in observed_locks, (
            "taking custody did not lock the repository's custody store"
        )

    def test_a_checkout_that_is_gone_cannot_be_held(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Otherwise the grant promises to protect nothing."""
        manager.remove_checkout_and_branch(checkout, force=True)

        with pytest.raises(CustodyUnavailableError, match="does not exist"):
            manager.take_custody(checkout, holder=HOLDER, reason=REASON)


class TestTheAuditOutlivesTheState:
    """Each write fails toward "still protected", never toward "silently gone"."""

    def test_a_take_whose_trail_fails_still_protects(
        self, manager: GitWorktreeManager, checkout: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            GitMetadataWorktreeCustody,
            "_append_trail",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(OSError):
            manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        assert manager.custody_of(checkout) is not None, (
            "the grant was written after its trail, so a failed trail left the "
            "checkout unprotected"
        )

    def test_a_release_whose_state_write_fails_stays_held_and_audited(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, monkeypatch
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        monkeypatch.setattr(
            GitMetadataWorktreeCustody,
            "_write",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(OSError):
            manager.release_custody(
                checkout, CustodyRelease(holder=HOLDER, reason="collected")
            )

        assert manager.custody_of(checkout) is not None
        trail = (repo / ".git" / CUSTODY_LOG).read_text()
        assert '"action": "release"' in trail, (
            "the release was not on record before the state changed"
        )


class TestOperatorReset:
    """A reset that reports success on a held checkout is worse than a failure.

    It clears the issue's state and requeues it, and the next launch reuses or
    resets the very checkout custody was taken to preserve (round 1 finding 1).
    """

    @pytest.mark.parametrize("from_scratch", [False, True], ids=["reuse", "scratch"])
    def test_a_held_checkout_stops_the_reset(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        checkout: Path,
        from_scratch: bool,
    ) -> None:
        issue_checkout = checkout.parent / f"{repo.name}-6410"
        _git(repo, "worktree", "add", "-b", "6410-work", str(issue_checkout))
        (issue_checkout / "work.md").write_text("the only copy\n")
        _git(issue_checkout, "add", "work.md")
        _git(issue_checkout, "commit", "-m", "work")
        manager.take_custody(issue_checkout, holder=HOLDER, reason=REASON)
        config = SimpleNamespace(worktree_base=checkout.parent, repo_root=repo)

        with pytest.raises(WorktreeInCustodyError):
            _remove_local_worktree(
                issue_number=6410,
                config=cast(Any, config),
                worktree_manager=manager,
                from_scratch=from_scratch,
            )

        assert (issue_checkout / "work.md").exists()
        assert "6410-work" in _branches(repo)

    @pytest.mark.parametrize("from_scratch", [False, True], ids=["reuse", "scratch"])
    def test_an_unreadable_held_checkout_still_stops_the_reset(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        tmp_path: Path,
        from_scratch: bool,
    ) -> None:
        """An inspection error is not an absent checkout or a successful reset.

        The early `Path.exists()` return meant reset went on to clear the
        issue's state without ever producing the custody refusal that is the
        whole point of this path (round 19 finding 2).
        """
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        issue_checkout = sealed / f"{repo.name}-6410"
        _git(repo, "worktree", "add", "-b", "6410-work", str(issue_checkout))
        (issue_checkout / "work.md").write_text("the only copy\n")
        _git(issue_checkout, "add", "work.md")
        _git(issue_checkout, "commit", "-m", "work")
        grant = manager.take_custody(issue_checkout, holder=HOLDER, reason=REASON)
        config = SimpleNamespace(worktree_base=sealed, repo_root=repo)

        with _unreadable(sealed):
            assert issue_checkout.exists() is False

            with pytest.raises(CustodyError):
                _remove_local_worktree(
                    issue_number=6410,
                    config=cast(Any, config),
                    worktree_manager=manager,
                    from_scratch=from_scratch,
                )

        assert (issue_checkout / "work.md").exists()
        assert manager.custody_of(issue_checkout) == grant
        assert "6410-work" in _branches(repo)


class TestCreatingOverAHeldCheckout:
    def test_a_fresh_create_does_not_delete_a_held_path(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The create path force-removes whatever is in the way.

        With reuse disabled, launching against a held existing checkout deleted
        and replaced it (round 1 finding 2).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            worktree_module._remove_existing_worktree_path(repo, checkout)  # noqa: SLF001

        assert (checkout / "finding.md").exists()


    def test_reuse_cannot_reset_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """Custody stopped REMOVAL but not destructive REPURPOSING.

        Reuse rebases, hard-resets and cleans -- which discards exactly the
        uncommitted evidence a grant is taken to protect (round 21 finding 1).
        """
        branch = "tech-lead-investigation-6410-abcdef123456"
        only_copy = checkout / "uncommitted-investigation.txt"
        only_copy.write_text("not committed anywhere\n")
        _git(repo, "remote", "add", "origin", str(repo))
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            worktree_module.create_worktree(
                repo,
                7275,
                "another launch",
                worktree_base=tmp_path / "new-worktrees",
                base_branch="main",
                branch_name=branch,
                enforce_hooks=False,
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
            )

        assert only_copy.read_text() == "not committed anywhere\n"
        assert _git(checkout, "branch", "--show-current").strip() == branch

    def test_reuse_disabled_cannot_detach_a_held_branch(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """Detaching frees the protected branch to be attached elsewhere."""
        branch = "tech-lead-investigation-6410-abcdef123456"
        grant = manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            worktree_module.create_worktree(
                repo,
                7276,
                "fresh launch",
                worktree_base=tmp_path / "fresh-worktrees",
                branch_name=branch,
                enforce_hooks=False,
                reuse_options=WorktreeReuseOptions(
                    disable_reuse=True, reuse_push_preflight=False
                ),
            )

        assert _git(checkout, "branch", "--show-current").strip() == branch
        assert manager.custody_of(checkout) == grant


    def test_preserved_reuse_refuses_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """Preserve mode SKIPPED the reset guard and still wrote runtime files.

        Round 21 guarded the reset only, so the one mode that exists to protect
        an investigation was the one mode that never asked (round 22 finding 1).
        """
        branch = "tech-lead-investigation-6410-abcdef123456"
        only_copy = checkout / "uncommitted-investigation.txt"
        only_copy.write_text("not committed anywhere\n")
        _git(repo, "remote", "add", "origin", str(repo))
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            worktree_module.create_worktree(
                repo,
                7278,
                "preserved launch",
                worktree_base=tmp_path / "new-worktrees",
                branch_name=branch,
                enforce_hooks=False,
                reuse_options=WorktreeReuseOptions(
                    preserve_branch=True, reuse_push_preflight=False
                ),
            )

        assert only_copy.read_text() == "not committed anywhere\n"
        assert _git(checkout, "branch", "--show-current").strip() == branch

    def test_completion_rebase_refuses_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A completion retry rebases the branch a grant protects."""
        from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

        (repo / "main-only.txt").write_text("advance main\n")
        _git(repo, "add", "main-only.txt")
        _git(repo, "commit", "-m", "advance main")
        before = _git(checkout, "rev-parse", "HEAD").strip()
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            GitWorkingCopy().rebase_on_branch(checkout, "main")

        assert _git(checkout, "rev-parse", "HEAD").strip() == before

    def test_collision_branch_switch_refuses_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """`checkout -B` moves HEAD off the protected branch."""
        from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

        original = _git(checkout, "branch", "--show-current").strip()
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            GitWorkingCopy().create_branch_from_current(checkout, "replacement")

        assert _git(checkout, "branch", "--show-current").strip() == original


class TestPruningAroundCustody:
    """``git worktree prune`` is repository-WIDE, so it is custody-sensitive.

    Git treats a checkout it cannot stat exactly like a missing one. An
    unreadable held checkout could therefore lose the registration keeping its
    branch attached to it, and a later ``worktree add`` could attach that branch
    elsewhere -- making the protection promise false while the grant still
    stands (round 20 finding 1).
    """

    def test_create_does_not_prune_an_unreadable_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, tmp_path: Path
    ) -> None:
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        held = sealed / "held"
        _git(repo, "worktree", "add", "-b", "sealed-investigation", str(held))
        (held / "finding.md").write_text("the only copy\n")
        manager.take_custody(held, holder=HOLDER, reason=REASON)
        _git(repo, "config", "gc.worktreePruneExpire", "now")

        with _unreadable(sealed):
            assert held.exists() is False
            with pytest.raises(
                CustodyUnavailableError, match="cannot prune worktree metadata"
            ):
                worktree_module.create_worktree(
                    repo,
                    7275,
                    "another launch",
                    worktree_base=tmp_path / "new-worktrees",
                    branch_name="sealed-investigation",
                    enforce_hooks=False,
                )

        registrations = _git(repo, "worktree", "list", "--porcelain")
        assert f"worktree {held}" in registrations
        assert (held / "finding.md").read_text() == "the only copy\n"

    def test_create_does_not_prune_a_held_checkout_with_missing_gitfile(
        self, manager: GitWorktreeManager, repo: Path, tmp_path: Path
    ) -> None:
        """Git prunes by the BACKLINK, not by whether the directory is there."""
        held = tmp_path / "held-without-gitfile"
        _git(repo, "worktree", "add", "-b", "missing-gitfile", str(held))
        admin = Path(_git(held, "rev-parse", "--absolute-git-dir").strip())
        manager.take_custody(held, holder=HOLDER, reason=REASON)
        (held / ".git").unlink()
        _git(repo, "config", "gc.worktreePruneExpire", "now")
        _git(repo, "branch", "next-launch")

        with pytest.raises(
            CustodyUnavailableError, match="cannot prune worktree metadata"
        ):
            worktree_module.create_worktree(
                repo,
                7277,
                "another launch",
                worktree_base=tmp_path / "new-worktrees",
                branch_name="next-launch",
                enforce_hooks=False,
            )

        registrations = _git(repo, "worktree", "list", "--porcelain")
        assert f"worktree {held}" in registrations
        assert admin.is_dir()

    def test_prune_refuses_a_held_checkout_pointing_at_a_sibling_admin(
        self, manager: GitWorktreeManager, repo: Path, tmp_path: Path
    ) -> None:
        """Right repository is not the same fact as right REGISTRATION.

        Checkout A's pointer can be replaced with B's, in this same repository.
        `git_common_dir` still answers correctly, and git then prunes A's own
        unclaimed registration (round 22 finding 2).
        """
        held = tmp_path / "held"
        sibling = tmp_path / "sibling"
        _git(repo, "worktree", "add", "-b", "held-branch", str(held))
        _git(repo, "worktree", "add", "-b", "sibling-branch", str(sibling))
        held_admin = Path(_git(held, "rev-parse", "--absolute-git-dir").strip())
        manager.take_custody(held, holder=HOLDER, reason=REASON)

        (held / ".git").write_bytes((sibling / ".git").read_bytes())
        _git(repo, "config", "gc.worktreePruneExpire", "now")
        _git(repo, "branch", "next-launch")

        with pytest.raises(
            CustodyUnavailableError, match="verify the git registration"
        ):
            worktree_module.create_worktree(
                repo,
                7279,
                "another launch",
                worktree_base=tmp_path / "new-worktrees",
                branch_name="next-launch",
                enforce_hooks=False,
            )

        registrations = _git(repo, "worktree", "list", "--porcelain")
        assert f"worktree {held}" in registrations
        assert held_admin.is_dir()
        assert "held-branch" in _git(repo, "branch", "--list", "held-branch")

    def test_removal_does_not_prune_past_an_unreadable_held_sibling(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        checkout: Path,
        tmp_path: Path,
    ) -> None:
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        held = sealed / "held"
        _git(repo, "worktree", "add", "-b", "sealed-investigation", str(held))
        manager.take_custody(held, holder=HOLDER, reason=REASON)

        with _unreadable(sealed):
            assert held.exists() is False
            with pytest.raises(
                CustodyUnavailableError, match="cannot prune worktree metadata"
            ):
                remove_checkout_path(
                    checkout,
                    force=True,
                    run_git=_git_in(repo),
                    repo_root=repo,
                    prune=True,
                )

        assert manager.custody_of(held) is not None

    def test_stale_recovery_does_not_call_an_unreadable_registration_missing(
        self, repo: Path, tmp_path: Path, monkeypatch
    ) -> None:
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        registered = sealed / "registered"
        registered.mkdir()
        monkeypatch.setattr(
            worktree_module,
            "_git_run",
            lambda *_args, **_kwargs: pytest.fail("prune was attempted"),
        )

        with _unreadable(sealed):
            assert registered.exists() is False
            with pytest.raises(
                CustodyUnavailableError,
                match="cannot determine whether registered worktree",
            ):
                worktree_module._recover_stale_branch_worktree_registration(
                    repo,
                    7275,
                    "protected",
                    "fatal: 'protected' is already used by worktree at "
                    f"'{registered}'",
                )


def test_the_store_satisfies_the_custody_port() -> None:
    """The port is a checked contract, not documentation of one."""
    store: WorktreeCustody = GitMetadataWorktreeCustody(Path("/nonexistent"))

    assert isinstance(store, GitMetadataWorktreeCustody)
    for method in ("take", "release", "held", "list_held", "inspect"):
        assert callable(getattr(store, method))


class TestTheFilesystemFallbacks:
    """A guard held only around the git attempt is a guard with a hole.

    Both of these delete the directory when git declines, and both used to do
    it after the lock was released -- so a hold taken in between was granted and
    then lost to the ``rmtree`` (round 2 findings 2 and 3).
    """

    def test_a_guard_holds_through_a_callers_whole_body(
        self, repo: Path, checkout: Path
    ) -> None:
        """Probed the way another PROCESS would, not raced against a timeout."""
        assert _custody_lock_is_held(repo) is False

        with custody_guard(checkout):
            assert _custody_lock_is_held(repo) is True
            shutil.rmtree(checkout)
            # Still held AFTER the destructive step: a take cannot be answered
            # between the failed git removal and the rmtree.
            assert _custody_lock_is_held(repo) is True

        assert _custody_lock_is_held(repo) is False

    def test_budgeted_validation_cleanup_asks(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Its command is built from varargs, which the old scan did not read."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        git = BudgetedValidationGit(repo, _RecordingRunner())

        with pytest.raises(WorktreeInCustodyError):
            git.remove_checkout(checkout)

        assert (checkout / "finding.md").exists()


class _RecordingRunner:
    """A command runner that must never be reached for a held checkout."""

    def run(self, *args: object, **kwargs: object):  # noqa: ANN201, ARG002
        raise AssertionError("a held checkout reached the command runner")


class TestCustodyOutlivesTheCheckoutsOwnMetadata:
    """A grant lives in the REPOSITORY, so the checkout cannot disown it.

    Resolving custody from the checkout alone answered "not in a repository,
    so not held" the moment its ``.git`` file went missing -- and forced
    removal then deleted a checkout somebody was holding (round 3 finding 2).
    """

    def test_a_held_checkout_that_lost_its_git_file_is_still_held(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        with pytest.raises(WorktreeInCustodyError):
            remove_checkout_path(
                checkout, force=True, run_git=None, repo_root=repo
            )

        assert (checkout / "finding.md").exists()

    def test_a_custody_file_that_exists_but_cannot_be_read_is_not_empty(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A dangling symlink raises the same error as an absent file.

        Reading that as "nothing is held" discards a grant that WAS recorded
        (round 3 finding 3).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        store = repo / ".git" / CUSTODY_FILE
        store.unlink()
        store.symlink_to(repo / ".git" / "nowhere.json")

        with pytest.raises(CustodyUnavailableError, match="cannot be read"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_an_absent_custody_file_really_is_an_empty_store(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Failing closed must not mean failing always."""
        manager.remove_checkout_and_branch(checkout, force=True)

        assert not checkout.exists()


class TestTheReentrantLockKey:
    def test_two_spellings_of_one_store_nest_without_deadlocking(
        self, repo: Path, checkout: Path
    ) -> None:
        """Keyed by text, a relative and an absolute path deadlock each other.

        The second ``flock`` is on a different descriptor for the same file, so
        it waits for a lock this thread already holds (round 3 finding 5).
        """
        common = git_common_dir(repo)
        assert common is not None
        relative = Path(os.path.relpath(common, Path.cwd()))

        with GitMetadataWorktreeCustody(common).guard(checkout):
            with GitMetadataWorktreeCustody(relative).guard(checkout):
                pass


class TestReuseCleanup:
    def test_delete_worktree_refuses_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """It used to delete the directory on ANY exception from git.

        So a custody refusal read as "git said no, try harder" and the checkout
        went, with the branch it carried still only on disk. It has no fallback
        of its own now: git first, then the directory, is what the removal
        owner does under one custody answer.
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            ValidateOrDeletePolicy().delete_worktree(checkout, repo)

        assert (checkout / "finding.md").exists()

    def test_delete_worktree_still_removes_what_nobody_holds(
        self, repo: Path, checkout: Path
    ) -> None:
        assert ValidateOrDeletePolicy().delete_worktree(checkout, repo) is True
        assert not checkout.exists()


    def test_a_trail_file_that_EXISTS_but_cannot_be_read_is_not_an_empty_audit(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A dangling audit symlink cannot erase an outstanding grant.

        The STATE file already refuses this (round 3 finding 3). The trail did
        not -- and the trail is read exactly when the state file is gone, so it
        is the last evidence a grant ever existed (round 13 finding 1).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        trail = repo / ".git" / CUSTODY_LOG
        trail.unlink()
        trail.symlink_to(repo / ".git" / "nothing-here.jsonl")
        (repo / ".git" / CUSTODY_FILE).unlink()

        with pytest.raises(CustodyUnavailableError, match="although something is there"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()


class TestRoundFourGaps:
    """Each of these deleted a held checkout by a route the owner did not see."""

    def test_a_checkout_that_lost_its_git_file_is_still_refused(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Finding [1], fixed rather than merely reported.

        A checkout with no ``.git`` file names no repository, so it cannot
        resolve its own store. The MANAGER knows which repository it serves --
        bound at composition, where the answer has always been available -- so
        the grant is found and the removal refused. Last round this deleted the
        checkout and reported a breach afterwards, which was the honest answer
        to the wrong question.
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        with pytest.raises(WorktreeInCustodyError):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()
        assert manager.breached_custody(repo) == ()

    def test_a_caller_that_knows_the_repository_still_refuses(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """And every caller inside the removal owner does know it."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        with pytest.raises(WorktreeInCustodyError):
            remove_checkout_path(
                checkout, force=True, run_git=None, repo_root=repo
            )

        assert (checkout / "finding.md").exists()

    def test_a_vanished_state_file_is_damage_when_the_trail_says_so(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The trail is append-only, so it outlives the state (finding 2)."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (repo / ".git" / CUSTODY_FILE).unlink()

        with pytest.raises(CustodyUnavailableError, match="trail still holds"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_vanished_state_file_after_a_release_is_just_empty(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Failing closed must not mean failing forever."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        manager.release_custody(
            checkout, CustodyRelease(holder=HOLDER, reason="collected")
        )
        (repo / ".git" / CUSTODY_FILE).unlink()

        manager.remove_checkout_and_branch(checkout, force=True)

        assert not checkout.exists()

    def test_the_e2e_sweep_leaves_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """It recursively deletes everything under the worktree base (finding 4)."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert (checkout / "finding.md").exists()

    def test_the_reviewer_markers_survive_a_custody_refusal(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """They come off BEFORE the removal, so a refusal must put them back.

        Without it the checkout survives but stops looking like a reviewer
        worktree, and reconciliation later calls it external (finding 5).
        """
        marker = checkout / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("wt-test\n")
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            remove_reviewer_worktree(
                ReviewerWorktree(path=checkout, coder_branch="6410-work"),
                force=True,
            )

        assert marker.read_text() == "wt-test\n"

    def test_the_cli_refuses_rather_than_traceback(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, capsys
    ) -> None:
        """This is the command an operator reaches for with work at stake."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        store = repo / ".git" / CUSTODY_FILE
        store.write_text("{ not json")

        exit_code = custody_cli(["list", "--repo-root", str(repo)])

        assert exit_code == 1
        assert "FAILED:" in capsys.readouterr().err


class TestWhatCustodyDetectsRatherThanPrevents:
    """Nothing stops a hand outside this codebase. Saying so is the honest part.

    A guardrail hunting every ``shutil.rmtree`` in every script is a search with
    no end -- four review rounds kept finding another one. So the owner prevents,
    and everything past its edge is DETECTED and reported.
    """

    def test_a_grant_whose_checkout_vanished_is_reported(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        shutil.rmtree(checkout)  # something outside this codebase

        breached = manager.breached_custody(repo)

        assert [grant.path for grant in breached] == [checkout]

    def test_the_operator_surface_says_so_and_exits_non_zero(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, capsys
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        shutil.rmtree(checkout)

        exit_code = custody_cli(["list", "--repo-root", str(repo)])

        assert exit_code == 1
        assert "GONE despite being held" in capsys.readouterr().out

    def test_a_held_checkout_that_is_still_there_is_not_a_breach(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        assert manager.breached_custody(repo) == ()


class TestRoundFiveGaps:
    def test_the_manager_carries_its_repository_into_the_orphan_path(
        self, repo: Path, checkout: Path
    ) -> None:
        """Bound at composition time, so the orphan path is not blind (finding 1).

        A checkout that lost its ``.git`` file names no repository, and custody
        lives in the repository's metadata. The manager knows which one it is.
        """
        bound = GitWorktreeManager(repo)
        bound.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        with pytest.raises(WorktreeInCustodyError):
            bound.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_pointer_that_disagrees_with_the_caller_fails_closed(
        self, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """A corrupted pointer that still parses would name an empty store.

        Nothing is held there, so the removal proceeds -- while the real
        repository's grant sits untouched (finding 2).
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _git(elsewhere, "init", ".")
        GitWorktreeManager(repo).take_custody(
            checkout, holder=HOLDER, reason=REASON
        )
        (checkout / ".git").write_text(f"gitdir: {elsewhere / '.git'}\n")

        with pytest.raises(CustodyUnavailableError, match="which store holds it"):
            GitWorktreeManager(repo).remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_damaged_trail_row_is_not_silently_skipped(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Skipping it could turn a recorded take into an empty store (finding 3)."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        trail = repo / ".git" / CUSTODY_LOG
        trail.write_text("{ not json\n")
        (repo / ".git" / CUSTODY_FILE).unlink()

        with pytest.raises(CustodyUnavailableError, match="unreadable row"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_release_is_not_consumed_by_a_removal_that_failed(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Consuming it first unprotects a checkout nothing removed (finding 6)."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        release = CustodyRelease(holder=HOLDER, reason="collected")

        with pytest.raises(RuntimeError, match="the removal itself failed"):
            with custody_guard(checkout, release):
                raise RuntimeError("the removal itself failed")

        assert manager.custody_of(checkout) is not None

    def test_a_release_is_consumed_when_the_removal_succeeds(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        manager.remove_checkout_and_branch(
            checkout,
            force=True,
            custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
        )

        assert manager.custody_of(checkout) is None

    def test_an_operator_can_release_a_grant_whose_checkout_is_gone(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, capsys
    ) -> None:
        """Otherwise a breach stays active forever (finding 4)."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        shutil.rmtree(checkout)
        assert manager.breached_custody(repo)

        exit_code = custody_cli(
            [
                "release",
                str(checkout),
                "--reason",
                "gone, closing the record",
                "--holder",
                HOLDER,
                "--repo-root",
                str(repo),
            ]
        )

        assert exit_code == 0, capsys.readouterr().err
        assert manager.breached_custody(repo) == ()

    def test_the_e2e_sweep_asks_with_the_repository_it_knows(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Its checkouts can lose their markers too (finding 5).

        ``repo_root`` is keyword-only with NO default, so this is not a
        courtesy a caller may forget: the e2e conftest, the only production
        caller, is a type error without it. Round 7 finding 1 was that it HAD a
        default and all three real call sites took it.
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert (checkout / "finding.md").exists()

    def test_the_e2e_sweep_does_not_delete_inside_a_held_checkout(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A missing ``.git`` must not turn the checkout into a container.

        In the flat layout the sweep treats a directory without ``.git`` as a
        per-session container and hands each CHILD to the forced removal owner.
        Custody has to protect those descendants too, or an investigation's
        uncommitted work is deleted while the checkout root and its grant
        survive -- looking, to anyone who checks, untouched (round 11 finding 1).
        """
        only_copy = checkout / "investigation" / "only-copy.md"
        only_copy.parent.mkdir()
        only_copy.write_text("not committed anywhere\n")
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert only_copy.read_text() == "not committed anywhere\n"
        assert manager.custody_of(checkout) is not None

    def test_the_e2e_sweep_does_not_unlink_a_symlink_inside_a_held_checkout(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        checkout: Path,
        tmp_path: Path,
    ) -> None:
        """Resolving a child must not erase its lexical held ancestor.

        The descendant test above uses an ordinary directory, where `resolve()`
        preserves the ancestry. A SYMLINK does not: resolving it moves the
        target outside the held checkout, the held ancestor disappears, and the
        fallback unlinks the alias -- inside a checkout custody is protecting
        (round 12 finding 2).
        """
        evidence = tmp_path / "external-evidence"
        evidence.mkdir()
        (evidence / "only-copy.md").write_text("not committed anywhere\n")
        link = checkout / "investigation-link"
        link.symlink_to(evidence, target_is_directory=True)
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert link.is_symlink(), (
            "the sweep unlinked an uncommitted path inside a held checkout"
        )
        assert (link / "only-copy.md").read_text() == "not committed anywhere\n"
        assert manager.custody_of(checkout) is not None

    def test_the_e2e_sweep_still_removes_what_nobody_holds(self, repo: Path, checkout: Path) -> None:
        """The premise: the sweep is a REMOVAL, and it goes through the owner."""
        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert not checkout.exists()


class TestTheFilesystemFallbackThroughProduction:
    """The git-fails-then-rmtree race, driven by the code that runs it.

    Earlier versions of this entered ``custody_guard`` by hand and did their own
    ``rmtree``, so moving the production fallback outside the guard left them
    green (round 5 finding 8). Here git genuinely refuses -- the worktree is
    locked -- so ``delete_worktree`` reaches its fallback for real, and a hold
    attempted in between must not be granted.
    """

    def test_a_hold_cannot_land_between_the_failed_git_removal_and_the_rmtree(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The control point is the caller's own git runner.

        Earlier versions started a thread and hoped it ran in the window; if the
        remover finished first the holder saw an absent path, recorded "too
        late", and the test passed through the regression. Adding a control
        point fixed the window but still proved the negative with a timeout, so
        the odds moved rather than went away (round 6 finding 6).

        There is no second thread and no timeout here. The runner is a PUBLIC
        seam the owner calls BETWEEN its two removal attempts, and at that exact
        moment the custody lock must already be held -- which is what makes any
        holder arriving in the window block until the removal has committed.
        ``flock`` is per file DESCRIPTION, so a fresh descriptor on the same
        lock file answers that question the way another process would.
        """
        observed: list[tuple[str, bool]] = []

        def git_refuses(argv: list[str]) -> str:
            observed.append((argv[0], _custody_lock_is_held(repo)))
            if argv[0] == "worktree" and argv[1] == "prune":
                return ""
            return "fatal: cannot remove a locked working tree"

        # ``prune`` is asked for so the owner calls the seam a SECOND time,
        # after the filesystem fallback has deleted the checkout. One
        # observation would only prove the lock was held before the delete; a
        # fallback lifted out of the guard would still pass it.
        outcome = remove_checkout_path(
            checkout, force=True, run_git=git_refuses, repo_root=repo, prune=True
        )

        assert [held for _, held in observed] == [True, True], (
            "the custody lock was open across the filesystem fallback, so a "
            f"hold could land on a checkout already being deleted: {observed}"
        )
        assert outcome.used_filesystem_fallback is True
        assert outcome.removed is True
        assert not checkout.exists()

    def test_the_fallback_still_refuses_a_checkout_held_first(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """And the lock means git cannot remove it either way."""
        _git(repo, "worktree", "lock", str(checkout))
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            ValidateOrDeletePolicy().delete_worktree(checkout, repo)

        assert (checkout / "finding.md").exists()


class TestTheDefaultWorktreeLayout:
    """The layout this repository actually uses (round 5 finding 9).

    ``~/dev/worktree/<repo>/<checkout>`` -- a sibling tree OUTSIDE the
    repository, which is why deriving a checkout's repository by walking up
    from it does not work and the caller has to say.
    """

    @pytest.fixture
    def sibling_checkout(self, repo: Path, tmp_path: Path) -> Path:
        base = tmp_path / "dev" / "worktree" / repo.name
        base.mkdir(parents=True)
        path = base / f"{repo.name}-6410"
        _git(repo, "worktree", "add", "-b", "6410-work", str(path))
        (path / "work.md").write_text("the only copy\n")
        _git(path, "add", "work.md")
        _git(path, "commit", "-m", "work")
        return path

    def test_a_held_sibling_checkout_is_refused(
        self, repo: Path, sibling_checkout: Path
    ) -> None:
        manager = GitWorktreeManager(repo)
        manager.take_custody(sibling_checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            manager.remove_checkout_and_branch(sibling_checkout, force=True)

        assert (sibling_checkout / "work.md").exists()
        assert "6410-work" in _branches(repo)

    def test_a_real_checkout_whose_registration_was_removed_is_still_held(
        self, repo: Path, sibling_checkout: Path
    ) -> None:
        """Not an arbitrary directory: a real linked checkout git forgot."""
        manager = GitWorktreeManager(repo)
        manager.take_custody(sibling_checkout, holder=HOLDER, reason=REASON)
        _git(repo, "worktree", "remove", "--force", str(sibling_checkout))
        sibling_checkout.mkdir(parents=True)
        (sibling_checkout / "work.md").write_text("recovered by hand\n")

        with pytest.raises(WorktreeInCustodyError):
            manager.remove_checkout_and_branch(sibling_checkout, force=True)

        assert (sibling_checkout / "work.md").exists()


class TestBranchRefDestruction:
    """A sweep that removes no directory can still destroy the only ref."""

    def test_test_teardown_cannot_delete_an_unregistered_held_branch(
        self,
        manager: GitWorktreeManager,
        repo: Path,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        branch = "1-test-custody"
        held = tmp_path / "dev" / "worktree" / repo.name / "held-test"
        held.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "-b", branch, str(held))
        (held / "only-copy.txt").write_text("protected commit\n")
        _git(held, "add", "only-copy.txt")
        _git(held, "commit", "-m", "only protected commit")
        grant = manager.take_custody(held, holder=HOLDER, reason=REASON)

        # The acceptance-criterion case: git has forgotten the checkout, but
        # the custody store and the local branch remain.
        _git(repo, "worktree", "remove", "--force", str(held))
        held.mkdir(parents=True)
        (held / "recovered-work.txt").write_text("operator recovery\n")

        monkeypatch.chdir(repo)
        teardown = _load_teardown_test_issues()

        assert teardown.cleanup_local_branches() == 0
        assert branch in _branches(repo), (
            "the sweep deleted the only protected ref"
        )
        assert (held / "recovered-work.txt").read_text() == "operator recovery\n"
        assert manager.custody_of(held) == grant


    def test_a_renamed_held_branch_is_still_recognised(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The recorded name is TEXT, and a rename makes it stale.

        `branch_guard` matched `grant.branch` exactly, so the branch a grant
        actually protects went unrecognised once renamed -- a guard that
        silently fails to match, which is worse than none (round 24 finding 1).

        Driven through the guard rather than the sweep on purpose: while the
        checkout is still REGISTERED git refuses the delete by itself, so an
        end-to-end test passes either way and proves nothing.
        """
        from issue_orchestrator.adapters.worktree.custody import (
            custody_branch_guard,
        )

        renamed = "tech-lead-investigation-6410-renamed"
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        _git(checkout, "branch", "-m", renamed)

        with pytest.raises(WorktreeInCustodyError):
            with custody_branch_guard(repo, renamed):
                pytest.fail("the renamed protected branch was not recognised")

    def test_a_detached_checkout_is_not_a_branch_to_ask_about(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """`rev-parse --abbrev-ref HEAD` answers "HEAD" for a detached checkout.

        That is not a branch to delete, and handing it to the branch guard as
        one raised and aborted an otherwise clean reviewer-worktree removal.

        Drives the production removal path too: asserting only the predicate
        stays green if `_delete_worktree_branch` stops using it and starts
        handing "HEAD" to custody again (round 25 finding 1).
        """
        from issue_orchestrator.adapters.worktree.custody import (
            names_a_local_branch,
        )

        assert names_a_local_branch("HEAD") is False
        assert names_a_local_branch(None) is False
        assert names_a_local_branch("   ") is False
        assert names_a_local_branch("refs/heads/") is False
        assert names_a_local_branch("6410-work") is True
        assert names_a_local_branch("refs/heads/6410-work") is True

        original_branch = _git(checkout, "branch", "--show-current").strip()
        _git(checkout, "checkout", "--detach")

        manager.remove_checkout_and_branch(checkout, force=True)

        assert not checkout.exists()
        assert original_branch in _branches(repo)

    def test_an_unreadable_held_branch_identity_refuses_deletion(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Unknown identity is not permission to delete somebody else's ref."""
        from issue_orchestrator.adapters.worktree.custody import (
            custody_branch_guard,
        )

        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        with pytest.raises(CustodyUnavailableError, match="readable branch identity"):
            with custody_branch_guard(repo, "some-other-branch"):
                pytest.fail("deletion proceeded with custody unable to answer")

    def test_a_refs_heads_spelling_still_matches_a_grant(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A guard that silently fails to match is worse than none."""
        from issue_orchestrator.adapters.worktree.custody import (
            custody_branch_guard,
        )

        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            with custody_branch_guard(
                repo, "refs/heads/tech-lead-investigation-6410-abcdef123456"
            ):
                pytest.fail("a fully-qualified ref name bypassed branch custody")


class TestRoundSixGaps:
    def test_a_release_is_not_consumed_by_a_removal_that_REPORTED_failure(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Not raising is not the same as having removed anything.

        A non-forced removal git declines returns ``removed=False`` and leaves
        the checkout there. Ending its grant would leave it standing and
        unprotected for the next forced cleanup -- the opposite of what the
        release asked for (round 6 finding 2).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        outcome = remove_checkout_path(
            checkout,
            force=False,
            run_git=lambda argv: "fatal: contains modified or untracked files",
            repo_root=repo,
            custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
        )

        assert outcome.removed is False
        assert (checkout / "finding.md").exists()
        assert manager.custody_of(checkout) is not None, (
            "the grant ended for a removal that never removed anything"
        )

    def test_a_release_is_not_consumed_when_git_claims_success_but_leaves_path(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A zero exit status is not proof that the checkout disappeared.

        The success branch ended the grant and reported ``removed=True`` on the
        strength of git's exit code alone, so a runner that succeeds while
        leaving the path standing left it unprotected for the next forced
        cleanup (round 17 finding 1).
        """
        grant = manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        outcome = remove_checkout_path(
            checkout,
            force=False,
            run_git=lambda _argv: None,
            repo_root=repo,
            custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
        )

        assert outcome.removed is False
        assert outcome.git_error == (
            f"git reported success but left checkout at {checkout}"
        )
        assert (checkout / "finding.md").exists()
        assert manager.custody_of(checkout) == grant

    def test_a_structurally_wrong_trail_row_is_damage(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A row this build cannot read is a grant it cannot account for.

        Skipping it is how the trail reports "nothing held" while something is
        (round 6 finding 3). Invalid JSON was already caught; a well-formed
        object with an unknown action was not.
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (repo / ".git" / CUSTODY_LOG).write_text(
            json.dumps({"action": "tkae", "path": str(checkout)}) + "\n"
        )
        (repo / ".git" / CUSTODY_FILE).unlink()

        with pytest.raises(CustodyUnavailableError, match="cannot read"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_hand_off_is_audited_before_the_removal_runs(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A process that dies mid-removal leaves an audited hand-off.

        Recorded only afterwards, a crash between the delete and the write left
        a breach with no actor and no reason -- nobody could tell an explicit
        hand-off from something that just vanished (round 6 finding 4).

        The claim is about order against the DESTRUCTIVE ACT, so the trail is
        read from inside the git seam, at the moment the removal runs. An
        earlier version appended to a throwaway list and then only asserted
        `release-intent` before `release` -- which stays true even if the intent
        were written after the checkout was already gone (round 7 finding 6).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        at_removal: list[list[str]] = []

        def git_removes(argv: list[str]) -> None:
            at_removal.append(_trail_actions(repo))
            shutil.rmtree(checkout)
            return None

        remove_checkout_path(
            checkout,
            force=True,
            run_git=git_removes,
            repo_root=repo,
            custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
        )

        assert at_removal == [["take", "release-intent"]], (
            "the hand-off was not audited before the removal ran: a crash here "
            f"would leave a breach nobody can account for ({at_removal})"
        )
        assert _trail_actions(repo) == ["take", "release-intent", "release"]


class TestRoundSevenGaps:
    def test_an_unattributed_release_row_does_not_clear_a_grant(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """A release nobody performed is damage, not a release.

        The row is structurally well formed -- a known action and a real path --
        so the round-6 shape check passed it, and the reconstructed grant was
        cleared. With the state file gone that is the whole answer, and forced
        cleanup deleted a held checkout on the strength of a line carrying no
        actor and no reason (round 7 finding 3).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (repo / ".git" / CUSTODY_LOG).write_text(
            json.dumps(
                {
                    "action": "take",
                    "path": str(checkout),
                    "holder": HOLDER,
                    "actor": HOLDER,
                    "reason": REASON,
                }
            )
            + "\n"
            + json.dumps({"action": "release", "path": str(checkout)})
            + "\n"
        )
        (repo / ".git" / CUSTODY_FILE).unlink()

        with pytest.raises(CustodyUnavailableError, match="with no"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_an_attributed_trail_still_reconstructs_the_release(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The premise: a properly attributed release DOES clear the grant."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        manager.release_custody(
            checkout, CustodyRelease(holder=HOLDER, reason="done looking")
        )
        (repo / ".git" / CUSTODY_FILE).unlink()

        manager.remove_checkout_and_branch(checkout, force=True)

        assert not checkout.exists()

    def test_custody_of_answers_for_a_checkout_that_lost_its_git_file(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The public read must use the SAME bound repository as the writes.

        Asking the checkout which repository it belongs to is exactly the
        question it can no longer answer, and answering "nobody holds it" is the
        false negative the binding exists to prevent (round 7 finding 5).
        """
        grant = manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        assert manager.custody_of(checkout) == grant


class TestRoundEightGaps:
    """A removal cannot take a held checkout out with its PARENT.

    Custody used to be an exact-key question. E2E runs with
    ``ORCHESTRATOR_WORKTREE_PER_SESSION=1``
    (``tests/e2e/fixtures/orchestrator_process.py`` defaults it to "1"), so the
    real layout is ``<base>/<session>/<checkout>`` -- and the sweep handed the
    SESSION directory to the owner. No grant named it, git declined to remove
    something that is not a worktree, and the forced fallback deleted the whole
    subtree (round 8 finding 1).
    """

    @pytest.fixture
    def nested(self, repo: Path, tmp_path: Path) -> Path:
        """The real per-session layout: base / session / checkout."""
        session = tmp_path / "worktree" / "issue-6410"
        session.mkdir(parents=True)
        path = session / f"{repo.name}-tech-lead-6410-{TOKEN}"
        _git(
            repo,
            "worktree",
            "add",
            "-b",
            f"tech-lead-investigation-6410-{TOKEN}",
            str(path),
        )
        (path / "finding.md").write_text("the only copy of this work\n")
        _git(path, "add", "finding.md")
        _git(path, "commit", "-m", "the finding")
        return path

    def test_removing_the_session_container_refuses(
        self, manager: GitWorktreeManager, repo: Path, nested: Path
    ) -> None:
        manager.take_custody(nested, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            remove_checkout_path(
                nested.parent, force=True, run_git=_git_in(repo), repo_root=repo
            )

        assert (nested / "finding.md").exists()

    def test_a_release_naming_the_parent_does_not_release_the_child(
        self, manager: GitWorktreeManager, repo: Path, nested: Path
    ) -> None:
        """Consent has to name what it discards.

        A release is an explicit hand-off of ONE grant. Applied to an ancestor
        it would discard every checkout beneath it, none of which the holder
        was asked about.
        """
        grant = manager.take_custody(nested, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            remove_checkout_path(
                nested.parent,
                force=True,
                run_git=_git_in(repo),
                repo_root=repo,
                custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
            )

        assert (nested / "finding.md").exists()
        assert manager.custody_of(nested) == grant

    def test_the_e2e_sweep_leaves_a_held_checkout_in_the_real_layout(
        self, manager: GitWorktreeManager, repo: Path, nested: Path
    ) -> None:
        """The sweep, at the layout E2E actually produces."""
        manager.take_custody(nested, holder=HOLDER, reason=REASON)

        cleanup_local_worktrees(nested.parent.parent, repo_root=repo)

        assert (nested / "finding.md").exists()
        assert (
            _git(repo, "branch", "--list", f"tech-lead-investigation-6410-{TOKEN}")
            .strip()
            .endswith(f"tech-lead-investigation-6410-{TOKEN}")
        )

    def test_a_held_checkout_does_not_shelter_its_siblings(
        self, manager: GitWorktreeManager, repo: Path, nested: Path
    ) -> None:
        """The sweep enumerates CHECKOUTS, which is a separate property.

        The guard already refuses to remove the container, so with
        container-level enumeration one held checkout retained the whole
        session directory -- every unheld sibling in it survived too, and the
        next run inherited them. Sweeping checkouts means a hold protects
        exactly what it names.
        """
        sibling = nested.parent / f"{repo.name}-tech-lead-6411-{TOKEN}"
        _git(
            repo,
            "worktree",
            "add",
            "-b",
            f"tech-lead-investigation-6411-{TOKEN}",
            str(sibling),
        )
        manager.take_custody(nested, holder=HOLDER, reason=REASON)

        cleanup_local_worktrees(nested.parent.parent, repo_root=repo)

        assert (nested / "finding.md").exists(), "the held checkout was removed"
        assert not sibling.exists(), (
            "an unheld checkout was sheltered by its held sibling"
        )

    def test_the_e2e_sweep_still_empties_the_real_layout(
        self, repo: Path, nested: Path
    ) -> None:
        """The premise: nothing held, and the container goes too."""
        base = nested.parent.parent

        cleanup_local_worktrees(base, repo_root=repo)

        assert not nested.exists()
        assert not nested.parent.exists()

    def test_the_orphan_path_asks_the_repository_the_caller_names(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The exported raw removal, not the already-safe manager path.

        This is the one case custody exists for -- a held checkout whose own
        ``.git`` file is gone -- and it used to reach a sentinel that consulted
        no store at all (round 8 finding 2).
        """
        grant = manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        with pytest.raises(WorktreeInCustodyError):
            worktree_module.remove_worktree(
                checkout, force=True, repo_root=repo
            )

        assert (checkout / "finding.md").exists()
        assert manager.custody_of(checkout) == grant

    def test_the_raw_removal_cannot_be_called_without_a_repository(self) -> None:
        """There is no longer a way to say "nobody knows" and proceed."""
        import inspect

        parameter = inspect.signature(
            worktree_module.remove_worktree
        ).parameters["repo_root"]

        assert parameter.default is inspect.Parameter.empty, (
            "repo_root has a default again, so a caller can omit it and remove "
            "a held checkout while believing it asked"
        )
        assert not hasattr(removal_module, "UNKNOWN_REPOSITORY"), (
            "the fail-open sentinel is back"
        )


class TestRoundNineGaps:
    def test_a_repo_root_that_is_not_a_repository_refuses(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """Deleting the sentinel closed the name, not the property.

        ``for_path`` still fell back to the CHECKOUT when a supplied
        ``repo_root`` did not resolve, so a path that is not a repository
        reproduced the old fail-open exactly: the checkout's ``.git`` is gone
        too, neither says, and the removal proceeds (round 9 finding 1).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()
        not_a_repo = tmp_path / "somewhere-else"
        not_a_repo.mkdir()

        with pytest.raises(CustodyUnavailableError, match="not a git repository"):
            remove_checkout_path(
                checkout, force=True, run_git=None, repo_root=not_a_repo
            )

        assert (checkout / "finding.md").exists()

    def test_a_relative_repository_finds_the_same_grant(
        self, repo: Path, checkout: Path, monkeypatch
    ) -> None:
        """A main checkout used to answer with a RELATIVE common directory.

        The linked-worktree branch resolves, the directory branch did not, so
        the same repository compared unequal to itself and a real grant was
        refused as a disagreement (round 9 finding 1).
        """
        monkeypatch.chdir(repo.parent)
        relative = GitWorktreeManager(Path(repo.name))
        absolute = GitWorktreeManager(repo)
        grant = absolute.take_custody(checkout, holder=HOLDER, reason=REASON)

        assert relative.custody_of(checkout) == grant

    def test_a_held_main_checkout_does_not_make_pruning_unknown(
        self, manager: GitWorktreeManager, repo: Path
    ) -> None:
        """A main checkout has a metadata DIRECTORY, not a pointer file.

        Round 22's backlink check read it as text first, so holding the main
        checkout turned every prune into a false refusal (round 24 finding 2).
        """
        grant = manager.take_custody(repo, holder=HOLDER, reason=REASON)

        with custody_prune_guard(repo):
            _git(repo, "worktree", "prune")

        assert manager.custody_of(repo) == grant
        assert (repo / "README.md").read_text() == "seed\n"

    def test_the_repository_itself_is_not_a_removal_target(
        self, manager: GitWorktreeManager, repo: Path
    ) -> None:
        """Its custody store and trail live under its own ``.git``.

        A forced removal deletes the audit trail it just wrote, so the release
        becomes unauditable at the moment it is exercised and settlement reads
        an empty store (round 9 finding 3). The linked-worktree fixtures cannot
        see this: their common directory is in the main checkout.
        """
        manager.take_custody(repo, holder=HOLDER, reason=REASON)

        with pytest.raises(ValueError, match="repository itself"):
            remove_checkout_path(
                repo,
                force=True,
                run_git=_git_in(repo),
                repo_root=repo,
                custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
            )

        assert (repo / "README.md").exists()
        assert manager.custody_of(repo) is not None, "the grant was discarded"
        assert (repo / ".git" / CUSTODY_LOG).exists(), "the audit trail was deleted"

    def test_the_main_checkout_is_refused_from_a_linked_repository_root(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The authoritative repo_root can itself be a linked checkout.

        Its common git directory still lives under the MAIN checkout, so
        comparing the removal target only with repo_root let the forced
        fallback delete that main checkout and the custody trail inside it
        (round 10 finding 1).
        """
        linked_root = tmp_path / "linked-control-root"
        _git(repo, "worktree", "add", "--detach", str(linked_root))
        manager = GitWorktreeManager(linked_root)
        manager.take_custody(repo, holder=HOLDER, reason=REASON)

        with pytest.raises(ValueError, match="custody metadata"):
            remove_checkout_path(
                repo,
                force=True,
                run_git=_git_in(linked_root),
                repo_root=linked_root,
                custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
            )

        assert (repo / "README.md").exists()
        assert manager.custody_of(repo) is not None, "the grant was discarded"
        assert (repo / ".git" / CUSTODY_LOG).exists(), "the audit trail was deleted"


class TestRoundFourteenHostileStorage:
    """The absent/unreadable columns were closed. This is the HOSTILE one.

    Thirteen rounds treated damaged state; round 14's enumeration asked what
    happens when the state is readable, well-formed and LYING.
    """

    def test_a_redirected_custody_directory_is_not_an_empty_store(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """Swapping the directory for a symlink used to mint a fresh store."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        real = repo / ".git" / CUSTODY_DIR
        decoy = tmp_path / "decoy-custody"
        decoy.mkdir()
        real.rename(tmp_path / "real-custody")
        real.symlink_to(decoy, target_is_directory=True)

        with pytest.raises(CustodyUnavailableError):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_a_replacement_git_directory_is_not_a_repository(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """An empty `.git` produced a new, empty custody store: nothing held."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (repo / ".git").rename(tmp_path / "real-git")
        (repo / ".git").mkdir()

        with pytest.raises(CustodyUnavailableError, match="git metadata"):
            remove_checkout_path(
                checkout, force=True, run_git=None, repo_root=repo
            )

        assert (checkout / "finding.md").exists()

    def test_a_valid_but_EMPTY_state_cannot_contradict_an_outstanding_take(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """`{}` is parseable. It is not therefore truthful.

        The append-only trail still holds the take, and a state file that omits
        it is damage however well-formed it is.
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (repo / ".git" / CUSTODY_FILE).write_text("{}")

        with pytest.raises(CustodyUnavailableError, match="omits grant"):
            manager.remove_checkout_and_branch(checkout, force=True)

        assert (checkout / "finding.md").exists()

    def test_an_empty_held_checkout_is_not_removed_as_a_container(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The one path that never asked custody anything (finding 2).

        An EMPTY held checkout that lost its `.git` is indistinguishable from a
        session container to a directory enumerator, and the container sweep
        removed it with a bare `rmdir`.
        """
        for child in checkout.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert checkout.exists(), "a held checkout was rmdir'd as a container"
        assert manager.custody_of(checkout) is not None


class TestRoundFifteenOverCorrection:
    """Fourteen rounds of closing fail-opens, then the swing back.

    Each of these is a refusal I added that was WRONG -- blocking legitimate
    work rather than hostile input. Fail-shut is quieter than fail-open and
    just as much a failure.
    """

    def test_a_hard_linked_custody_file_is_still_readable(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path, tmp_path: Path
    ) -> None:
        """Round 14 rejected st_nlink != 1 on backwards reasoning.

        Every hard link names the SAME inode, so they all flock the same file.
        The check bought no integrity and broke hard-link snapshots -- which is
        how a great many backup tools work.
        """
        grant = manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        state = repo / ".git" / CUSTODY_FILE
        os.link(state, tmp_path / "backup-snapshot.json")

        assert manager.custody_of(checkout) == grant

    def test_a_relocated_object_database_is_still_a_repository(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """`objects` may legitimately live elsewhere via GIT_OBJECT_DIRECTORY.

        HEAD is what a valid repository must have; requiring the default
        `objects` path refused real repositories.
        """
        elsewhere = tmp_path / "relocated-objects"
        (repo / ".git" / "objects").rename(elsewhere)

        assert git_common_dir(repo) == (repo / ".git").resolve()


class TestEveryManagerNamesItsRepository:
    """No ``GitWorktreeManager()`` anywhere is built without a repository.

    Round 6 made the argument required precisely so an unbound manager could not
    answer "unheld" for a checkout that lost its ``.git`` file. Round 7 found the
    migration incomplete: eight call sites in the test suites still built one
    with no argument, raising ``TypeError`` before their behaviour ran, and
    pyright covers ``src`` only (round 7 finding 7).
    """

    def test_no_call_site_omits_the_repository(self) -> None:
        root = Path(__file__).resolve().parents[2]
        unbound: list[str] = []
        for directory in ("src", "tests", "scripts", "tools", "repo-specific"):
            for path in sorted((root / directory).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "GitWorktreeManager"
                        and not node.args
                        and not node.keywords
                    ):
                        rel = path.relative_to(root)
                        unbound.append(f"{rel}:{node.lineno}")
        assert unbound == [], (
            "these build a GitWorktreeManager with no repository, so it cannot "
            f"answer for a checkout that lost its .git file: {unbound}"
        )


def _trail_entries(repo: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (repo / ".git" / CUSTODY_LOG).read_text().splitlines()
        if line.strip()
    ]


def _trail_actions(repo: Path) -> list[str]:
    return [entry["action"] for entry in _trail_entries(repo)]
