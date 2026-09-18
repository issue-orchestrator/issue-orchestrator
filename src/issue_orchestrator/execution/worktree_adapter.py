"""WorktreeManager adapter implementation.

Implements the WorktreeManager port using the git worktree implementation.
"""

from pathlib import Path
from .git_tools import run_git

from ..ports.worktree_custody import (
    CustodyGrant,
    CustodyRelease,
    CustodyUnavailableError,
)
from ..ports.worktree_manager import (
    RegisteredWorktree,
    ReviewerHeadOwnership,
    WorktreeInfo,
    WorktreeReuseOptions,
)
from ..adapters.worktree._worktree_runtime import read_reviewer_head_ownership
from ..adapters.worktree._worktree import (
    can_remove_without_user_changes,
    create_worktree,
    get_worktree_branch,
    remove_worktree,
    extract_issue_number_from_branch,
    list_registered_worktrees,
)
from ..adapters.worktree.custody import GitMetadataWorktreeCustody


class GitWorktreeManager:
    """Git-based implementation of WorktreeManager.

    Wraps the _worktree_impl functions to implement the port protocol.

    Bound to a repository when the composition root knows one, and that is not
    a convenience: custody lives in the REPOSITORY's metadata, and a checkout
    that has lost its ``.git`` file names no repository at all. Without this
    the orphan path could not find the grant that was protecting it and deleted
    the checkout (#7274 round 4/5 finding 1). Every removal this manager makes
    therefore carries the repository it belongs to.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = Path(repo_root)

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options: WorktreeReuseOptions | None = None,
        worktree_name: str | None = None,
    ) -> WorktreeInfo:
        """Create a new git worktree for an issue."""
        path, branch, reuse_status, reuse_reason, rebase_failed, uncommitted_discarded, commits_discarded = create_worktree(
            repo_root=repo_root,
            issue_number=issue_number,
            issue_title=issue_title,
            worktree_base=worktree_base,
            base_branch=base_branch,
            enforce_hooks=enforce_hooks,
            pre_push_hook=pre_push_hook,
            branch_name=branch_name,
            reuse_options=reuse_options,
            seed_ref=seed_ref,
            worktree_name=worktree_name,
        )
        return WorktreeInfo(
            path=path,
            branch_name=branch,
            reuse_status=reuse_status,
            reuse_reason=reuse_reason,
            rebase_failed=rebase_failed,
            uncommitted_discarded=uncommitted_discarded,
            commits_discarded=commits_discarded,
        )

    def remove_checkout(
        self,
        worktree_path: Path,
        *,
        force: bool = False,
        custody_release: CustodyRelease | None = None,
    ) -> None:
        """Remove a git worktree checkout while preserving its branch."""
        remove_worktree(
            worktree_path,
            force=force,
            delete_branch=False,
            custody_release=custody_release,
            repo_root=self._repo_root,
        )

    def remove_checkout_and_branch(
        self,
        worktree_path: Path,
        *,
        force: bool = False,
        custody_release: CustodyRelease | None = None,
    ) -> None:
        """Remove a disposable worktree checkout and its local branch."""
        remove_worktree(
            worktree_path,
            force=force,
            delete_branch=True,
            custody_release=custody_release,
            repo_root=self._repo_root,
        )

    def take_custody(
        self, worktree_path: Path, *, holder: str, reason: str
    ) -> CustodyGrant:
        """Hold a checkout so no removal path can discard it."""
        if not worktree_path.exists():
            # Said here for the message: resolving the store first would report
            # "not inside a git repository", which is true of a deleted
            # checkout and tells an operator nothing. The authoritative check
            # is inside the store's lock, where it closes the race.
            raise CustodyUnavailableError(
                f"{worktree_path} does not exist, so it cannot be held"
            )
        custody = self._custody(worktree_path)
        return custody.take(
            worktree_path,
            branch=get_worktree_branch(worktree_path),
            holder=holder,
            reason=reason,
        )

    def release_custody(
        self, worktree_path: Path, release: CustodyRelease
    ) -> CustodyGrant | None:
        """End a grant without removing anything."""
        return self._custody(worktree_path).release(worktree_path, release)

    def custody_of(self, worktree_path: Path) -> CustodyGrant | None:
        """Who holds this checkout, or None.

        Through the same bound repository as every other method here. Asking
        the CHECKOUT which repository it belongs to is exactly the question a
        checkout that has lost its ``.git`` file cannot answer, and answering
        "nobody holds it" there is the false negative the binding exists to
        prevent (round 7 finding 5).
        """
        return self._custody(worktree_path).held(worktree_path)

    def checkouts_in_custody(self, repo_root: Path) -> tuple[CustodyGrant, ...]:
        """Every checkout of ``repo_root`` held for a person, oldest first."""
        return self._custody(repo_root).list_held()

    def breached_custody(self, repo_root: Path) -> tuple[CustodyGrant, ...]:
        """Grants whose checkout is gone: something removed it anyway."""
        return self._custody(repo_root).breached()

    def _custody(self, path: Path) -> GitMetadataWorktreeCustody:
        """Fails rather than reporting a path as unheld it cannot resolve."""
        custody = GitMetadataWorktreeCustody.for_path(path, self._repo_root)
        if custody is None:
            # A port error, not an adapter one: the caller asked about custody
            # and the answer is that it cannot be determined here.
            raise CustodyUnavailableError(
                f"{path} is not inside a git repository, so nothing can be "
                "held there"
            )
        return custody

    def can_remove_without_user_changes(self, worktree_path: Path) -> bool:
        """Return true when forced removal would not discard user changes."""
        return can_remove_without_user_changes(worktree_path)

    def is_commit_retained_by_branch(self, worktree_path: Path, head_sha: str, branch: str) -> bool:
        """No proof on missing refs, missing objects, or unavailable Git."""
        retained, _ = run_git(["merge-base", "--is-ancestor", head_sha, f"refs/heads/{branch}"], cwd=worktree_path)
        return retained

    def read_reviewer_head_ownership(
        self, worktree_path: Path
    ) -> ReviewerHeadOwnership:
        """Read the lifecycle-owned detached reviewer tip."""
        return read_reviewer_head_ownership(worktree_path)

    def list_registered(self, repo_root: Path) -> tuple[RegisteredWorktree, ...]:
        """Return worktrees registered in git metadata."""
        return list_registered_worktrees(repo_root)

    def extract_issue_number(self, branch_name: str) -> int | None:
        """Extract issue number from a branch name."""
        return extract_issue_number_from_branch(branch_name)
