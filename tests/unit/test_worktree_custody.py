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
import re
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.custody import (
    CUSTODY_FILE,
    CUSTODY_LOG,
    GitMetadataWorktreeCustody,
    git_common_dir,
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
    ) -> None:
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

        with pytest.raises(ValueError, match="unreadable"):
            GitMetadataWorktreeCustody.for_path(checkout).held(checkout)  # type: ignore[union-attr]

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


def _calls_require_no_custody(module: Path) -> bool:
    """A CALL, not a mention: an import left behind proves nothing."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    return any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", ""))
        == "require_no_custody"
        for node in ast.walk(tree)
    )


class TestNoRemovalPathCanSkipTheCheck:
    """Custody enforced at four call sites is custody a fifth one skips.

    The shape of #7274 was not a missing check; it was four independent
    removals and a lock only one of them honoured. So this reads the source: a
    module that removes a WORKTREE must ask ``require_no_custody`` first, and a
    new one that does not fails here rather than in production.
    """

    #: Modules that run ``git worktree remove`` against a workspace they made
    #: themselves and nobody can hold: a validation lane's scratch checkout, a
    #: publication workspace, an E2E fixture, a doctor repair. Each is listed
    #: with the reason it is not a lifecycle worktree.
    OWN_EPHEMERAL_WORKSPACES = {
        "adapters/budgeted_validation_git.py": "validation lane scratch checkout",
        "execution/publication_workspace.py": "publication workspace",
        "infra/e2e_worktree.py": "E2E fixture repository",
        "infra/doctor/checks/guardrails.py": "doctor repair of its own probe",
        "adapters/git/git_cli.py": "the git command surface itself",
    }

    def _removal_modules(self) -> set[str]:
        """Every module that issues a worktree removal, by repo-relative path.

        Read as a git ARGUMENT LIST -- ``"worktree"`` immediately followed by
        ``"remove"`` -- with whitespace collapsed first, so a call split over
        several lines counts and the words appearing separately in prose does
        not.
        """
        src = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
        argv = re.compile(r"""(['"])worktree\1\s*,\s*(['"])remove\2""")
        return {
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if argv.search(path.read_text(encoding="utf-8"))
        }

    def test_every_removal_module_asks_or_is_listed(self) -> None:
        src = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
        unguarded = []
        for relative in sorted(self._removal_modules()):
            if relative in self.OWN_EPHEMERAL_WORKSPACES:
                continue
            if not _calls_require_no_custody(src / relative):
                unguarded.append(relative)

        assert not unguarded, (
            "these modules remove a worktree without asking whether it is in "
            f"custody: {unguarded}. Call require_no_custody first, or list the "
            "module in OWN_EPHEMERAL_WORKSPACES with the reason nobody can "
            "hold what it removes."
        )

    def test_the_listed_exemptions_still_exist(self) -> None:
        """An exemption for a deleted module is a rule nobody is following."""
        src = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
        missing = [
            relative
            for relative in self.OWN_EPHEMERAL_WORKSPACES
            if not (src / relative).exists()
        ]

        assert not missing, f"exempted modules that no longer exist: {missing}"

    def test_the_scan_finds_the_paths_it_is_supposed_to_guard(self) -> None:
        """A scan that matches nothing would pass by finding no work."""
        found = self._removal_modules()

        assert "adapters/worktree/_worktree.py" in found
        assert "execution/reviewer_worktree.py" in found


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
