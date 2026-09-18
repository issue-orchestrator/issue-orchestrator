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
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from issue_orchestrator.adapters.worktree.custody import (
    CUSTODY_FILE,
    CUSTODY_LOG,
    GitMetadataWorktreeCustody,
    custody_guard,
    git_common_dir,
)
import issue_orchestrator.adapters.worktree._worktree as worktree_module
from issue_orchestrator.control.maintenance import _remove_local_worktree
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
    CustodyRelease,
    WorktreeInCustodyError,
)
from issue_orchestrator.adapters.worktree.worktree_policy import (
    ValidateOrDeletePolicy,
)
from issue_orchestrator.execution.reviewer_worktree import (
    ReviewerWorktree,
    remove_reviewer_worktree,
)
from issue_orchestrator.ports.worktree_manager import WORKTREE_ID_MARKER

HOLDER = "operator"
REASON = "investigation #6410 left commits only on this branch"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


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
def manager() -> GitWorktreeManager:
    return GitWorktreeManager()


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

        with pytest.raises(Exception, match="not inside a git repository"):
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

        held = GitWorktreeManager().custody_of(checkout)

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

        assert [entry["action"] for entry in trail] == ["take", "release"]
        assert trail[-1]["reason"] == "collected"
        assert trail[-1]["branch"] == "tech-lead-investigation-6410-abcdef123456"

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
        return WorktreeAuditOwner(GitWorktreeManager()).audit(
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


def _calls(node: ast.AST, name: str) -> bool:
    """A CALL, not a mention: an import left behind proves nothing."""
    return any(
        isinstance(child, ast.Call)
        and getattr(child.func, "id", getattr(child.func, "attr", "")) == name
        for child in ast.walk(node)
    )


def _builds_removal_argv(node: ast.AST) -> int | None:
    """The line where this function spells ``worktree`` then ``remove``."""
    for child in ast.walk(node):
        if not isinstance(child, (ast.List, ast.Tuple)):
            continue
        words = [
            element.value
            for element in child.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        for first, second in zip(words, words[1:]):
            if first == "worktree" and second == "remove":
                return child.lineno
    return None


class TestNoRemovalPathCanSkipTheCheck:
    """Custody enforced at four call sites is custody a fifth one skips.

    The shape of #7274 was not a missing check; it was several independent
    removals and a lock only one of them honoured. So this reads the source
    FUNCTION BY FUNCTION: anything that builds a ``git worktree remove``
    argument list must wrap it in ``custody_guard``, and a new one that does
    not fails here rather than in production.

    Per function, not per module: the first version of this guard looked for
    the call anywhere in the file, so deleting the call while leaving the
    import kept it green -- and it missed the create path entirely, which was
    round 1 finding [2].
    """

    #: Removal helpers whose guard is held by the caller named here, because
    #: the lock has to span the whole removal and the helper is the second half
    #: of one. Each named guard is itself checked below.
    GUARDED_BY_CALLER = {
        "adapters/worktree/_worktree.py::_clear_existing_worktree_path": (
            "_remove_existing_worktree_path"
        ),
        "adapters/worktree/_worktree.py::_remove_worktree_path": "remove_worktree",
    }

    def _src(self) -> Path:
        return Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"

    def _removal_functions(self) -> dict[str, ast.FunctionDef]:
        """Every function that builds a ``git worktree remove`` argument list.

        Read as a LIST LITERAL rather than a substring, so a call split over
        several lines counts and the two words appearing in prose do not.
        """
        found: dict[str, ast.FunctionDef] = {}
        for path in sorted(self._src().rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _builds_removal_argv(node):
                    found[f"{path.relative_to(self._src())}::{node.name}"] = node
        return found

    def test_every_removal_function_guards_its_removal(self) -> None:
        unguarded = [
            name
            for name, node in self._removal_functions().items()
            if name not in self.GUARDED_BY_CALLER and not _calls(node, "custody_guard")
        ]

        assert not unguarded, (
            "these functions remove a worktree without holding custody: "
            f"{unguarded}. Wrap the removal in custody_guard, or -- if the "
            "guard must span a caller's whole operation -- name that caller in "
            "GUARDED_BY_CALLER."
        )

    def test_each_delegated_guard_really_guards(self) -> None:
        """A helper is only exempt if the function it names actually holds one."""
        functions = {}
        for path in sorted(self._src().rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{path.relative_to(self._src())}::{node.name}"] = node

        for helper, guardian in self.GUARDED_BY_CALLER.items():
            module = helper.split("::", 1)[0]
            node = functions.get(f"{module}::{guardian}")
            assert node is not None, f"{helper} names a guard that does not exist"
            assert _calls(node, "custody_guard"), (
                f"{helper} is exempt because {guardian} guards it, but "
                f"{guardian} holds no custody guard"
            )

    def test_the_scan_finds_the_functions_it_is_supposed_to_guard(self) -> None:
        """A scan that matches nothing would pass by finding no work."""
        found = self._removal_functions()

        assert "adapters/worktree/_worktree.py::_remove_worktree_path" in found
        assert "execution/reviewer_worktree.py::remove_reviewer_worktree" in found
        assert "adapters/git/git_cli.py::worktree_remove" in found
        assert len(found) >= 8, f"only {len(found)} removal functions found"


class TestThePathsThatDoNotUseTheSeam:
    """Two removals do not go through ``remove_worktree``. Both still ask."""

    def test_reuse_cleanup_does_not_rmtree_past_a_refusal(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """Its fallback deletes the directory on ANY exception from git.

        Before this, a custody refusal read as "git said no, try harder" and
        the checkout was removed by ``shutil.rmtree`` -- with the branch it
        carried still only on disk.
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        with pytest.raises(WorktreeInCustodyError):
            ValidateOrDeletePolicy().delete_worktree(checkout, repo)

        assert (checkout / "finding.md").exists()

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

    def test_a_take_waits_for_a_removal_that_is_already_underway(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        """The guard holds the answer for as long as the removal takes."""
        order: list[str] = []
        inside = threading.Event()
        answered = threading.Event()

        def take_while_it_is_open() -> None:
            inside.wait(timeout=5)
            try:
                manager.take_custody(checkout, holder=HOLDER, reason=REASON)
                order.append("held")
            except CustodyUnavailableError:
                order.append("too late")
            answered.set()

        taker = threading.Thread(target=take_while_it_is_open)
        taker.start()
        try:
            with custody_guard(checkout):
                inside.set()
                # The take is running now. If it could answer here, an operator
                # would be told the checkout is protected while this body is
                # already committed to removing it.
                assert not answered.wait(timeout=0.5), (
                    "a take was answered inside an open removal"
                )
                order.append("removing")
                shutil.rmtree(checkout)
        finally:
            taker.join(timeout=5)

        assert order == ["removing", "too late"]

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


def test_the_store_satisfies_the_custody_port() -> None:
    """The port is a checked contract, not documentation of one."""
    store: WorktreeCustody = GitMetadataWorktreeCustody(Path("/nonexistent"))

    assert isinstance(store, GitMetadataWorktreeCustody)
    for method in ("take", "release", "held", "list_held"):
        assert callable(getattr(store, method))
