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
    CustodyRelease,
    WorktreeInCustodyError,
)
from issue_orchestrator.adapters.worktree.removal import remove_checkout_path
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
    #: ``tests`` is excluded deliberately: a test building a worktree fixture is
    #: not the orchestrator removing somebody's checkout, and the one test
    #: helper that DID sweep real worktrees (the E2E cleanup) asks custody and
    #: is covered by a test of its own rather than by this scan.
    #:
    #: What this does NOT do is hunt ``shutil.rmtree``. Any line anywhere can
    #: delete a directory, and a guardrail chasing that is a search with no end
    #: -- four review rounds each found one more. The owner PREVENTS what goes
    #: through it; ``GitMetadataWorktreeCustody.breached`` DETECTS what does not.
    SEARCHED = ("src", "scripts", "tools", "repo-specific")

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
    DESTRUCTIVE = ("_remove_with_git", "_delete_path")

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
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                _is_call(item.context_expr, "custody_guard") for item in node.items
            )
        ]
        assert guarded, "the removal owner does not ask custody at all"

        inside = {
            name
            for block in guarded
            for node in ast.walk(block)
            for name in self.DESTRUCTIVE
            if _is_call(node, name)
        }
        called = {
            name
            for node in ast.walk(tree)
            for name in self.DESTRUCTIVE
            if _is_call(node, name)
        }

        assert called == set(self.DESTRUCTIVE), (
            f"the owner no longer performs {set(self.DESTRUCTIVE) - called}; "
            "this guard is watching the wrong names"
        )
        assert inside == called, (
            f"these run OUTSIDE the custody guard: {sorted(called - inside)}"
        )

    def test_the_scan_reads_both_ways_a_command_is_built(self) -> None:
        """A list literal and a vararg call are the same removal."""
        as_list = ast.parse('git.run(repo, ["worktree", "remove", str(p)])')
        as_varargs = ast.parse('self.git("worktree", "remove", "--force", str(p))')

        assert _removal_lines(as_list) == [1]
        assert _removal_lines(as_varargs) == [1]


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


class TestTheFilesystemFallbacks:
    """A guard held only around the git attempt is a guard with a hole.

    Both of these delete the directory when git declines, and both used to do
    it after the lock was released -- so a hold taken in between was granted and
    then lost to the ``rmtree`` (round 2 findings 2 and 3).
    """

    def test_a_guard_holds_through_a_callers_whole_body(
        self, manager: GitWorktreeManager, repo: Path, checkout: Path
    ) -> None:
        held: list[str] = []
        taking = threading.Thread(
            target=lambda: held.append(_take_or_fail(manager, checkout))
        )

        with custody_guard(checkout):
            taking.start()
            # The policy's whole body runs under a guard, so a take cannot be
            # answered anywhere inside it -- including between the failed git
            # removal and the rmtree.
            assert not held
            taking.join(timeout=0.5)
            assert not held, "a take was answered inside an open removal"

        taking.join(timeout=5)
        assert held == ["held"]

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


def _take_or_fail(manager: GitWorktreeManager, path: Path) -> str:
    try:
        manager.take_custody(path, holder=HOLDER, reason=REASON)
        return "held"
    except CustodyUnavailableError:
        return "too late"


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

        cleanup_local_worktrees(checkout.parent)

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
        """Its checkouts can lose their markers too (finding 5)."""
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)
        (checkout / ".git").unlink()

        cleanup_local_worktrees(checkout.parent, repo_root=repo)

        assert (checkout / "finding.md").exists()


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

        Earlier versions started a thread and hoped it ran in the window; if
        the remover finished first the holder saw an absent path, recorded
        "too late", and the test passed through the regression (round 6
        finding 6). The runner is a PUBLIC seam the owner calls between its two
        attempts, so failing git there puts the holder exactly where it needs
        to be, every time.
        """
        order: list[str] = []
        answered = threading.Event()

        def hold_now() -> None:
            try:
                manager.take_custody(checkout, holder=HOLDER, reason=REASON)
                order.append("held")
            except CustodyError:
                order.append("refused")
            answered.set()

        def git_refuses(argv: list[str]) -> str:
            # Inside the guard, after git has declined and before the
            # filesystem fallback runs.
            holder = threading.Thread(target=hold_now)
            holder.start()
            assert not answered.wait(timeout=0.5), (
                "a hold was answered while a removal was already underway"
            )
            order.append("git refused")
            holder.join(timeout=5)
            return "fatal: cannot remove a locked working tree"

        outcome = remove_checkout_path(
            checkout, force=True, run_git=git_refuses, repo_root=repo
        )

        assert outcome.used_filesystem_fallback is True
        assert outcome.removed is True
        assert not checkout.exists()
        assert order[0] == "git refused", (
            f"the hold was answered before the removal committed: {order}"
        )

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

        Recorded only afterwards, a crash between the delete and the write
        left a breach with no actor and no reason -- nobody could tell an
        explicit hand-off from something that just vanished (round 6 finding 4).
        """
        manager.take_custody(checkout, holder=HOLDER, reason=REASON)

        remove_checkout_path(
            checkout,
            force=True,
            run_git=lambda argv: _trail_actions(repo).append("removal ran") or None,
            repo_root=repo,
            custody_release=CustodyRelease(holder=HOLDER, reason="collected"),
        )

        actions = [entry["action"] for entry in _trail_entries(repo)]
        assert actions.index("release-intent") < actions.index("release")


def _trail_entries(repo: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (repo / ".git" / CUSTODY_LOG).read_text().splitlines()
        if line.strip()
    ]


def _trail_actions(repo: Path) -> list[str]:
    return [entry["action"] for entry in _trail_entries(repo)]
