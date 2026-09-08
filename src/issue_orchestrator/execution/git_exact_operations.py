"""Immutable pins and explicit compare-and-set pushes over the Git port."""

import os

from ..adapters.git.git_cli import GIT_ENV_STRIP
from pathlib import Path

from ..domain.exact_git import (
    ExactPushAuthenticationError,
    ExactPushDestination,
    ExactPushOutcome,
    ExactPushResult,
    RefPinOutcome,
    RetainedRef,
)
from ..domain.validated_work import require_sha
from ..domain.validated_work_store import AncestryRelation
from ..ports.git import Git, GitError
from .git_push_operations import GitAuthEnvProvider
from .git_exact_context import ExactPushContextOwner, require_remote

_PREFIXES = ("refs/issue-orchestrator/validated/", "refs/issue-orchestrator/observed/")


class GitExactOperations:
    def __init__(self, git: Git, auth: GitAuthEnvProvider | None) -> None:
        self._git = git
        self._auth = auth

    def _check_ref(self, repository: Path, ref: str) -> None:
        if not ref.startswith(_PREFIXES):
            raise ValueError("ref must belong to validated-work retention")
        self._git.run(repository, ["check-ref-format", ref])

    def _commit_exists(self, repository: Path, sha: str, env: dict[str, str] | None = None) -> bool:
        require_sha(sha)
        result = self._git.run(
            repository, ["--no-replace-objects", "cat-file", "-t", sha], check=False, env=env
        )
        return result.returncode == 0 and result.stdout.strip() == "commit"

    def _read_pin(self, repository: Path, ref: str) -> str | None:
        # Symbolic pins are mutable indirection, including dangling symrefs.
        symbolic = self._git.run(repository, ["symbolic-ref", "-q", ref], check=False)
        if symbolic.returncode == 0:
            raise ValueError("retention ref must not be symbolic")
        if symbolic.returncode != 1:
            raise GitError(symbolic)
        result = self._git.run(
            repository, ["show-ref", "--verify", "--hash", ref], check=False
        )
        if result.returncode == 1:
            return None
        if result.returncode:
            # show-ref returns 128 for a missing exact ref on older Git.
            probe = self._git.run(
                repository, ["for-each-ref", "--format=%(refname)", ref]
            )
            if not probe.stdout.strip():
                return None
            raise GitError(result)
        return result.stdout.strip()

    def pin_ref(self, repository: Path, *, ref: str, sha: str) -> RefPinOutcome:
        self._check_ref(repository, ref)
        require_sha(sha)
        if not self._commit_exists(repository, sha):
            return RefPinOutcome.OBJECT_MISSING
        existing = self._read_pin(repository, ref)
        if existing is not None:
            return (
                RefPinOutcome.ALREADY_PINNED
                if existing == sha
                else RefPinOutcome.CONFLICT
            )
        result = self._git.run(
            repository, ["update-ref", "--no-deref", ref, sha, "0" * 40], check=False
        )
        if result.returncode == 0:
            return RefPinOutcome.PINNED
        return (
            RefPinOutcome.ALREADY_PINNED
            if self.verify_ref(repository, ref=ref, sha=sha)
            else RefPinOutcome.CONFLICT
        )

    def verify_ref(self, repository: Path, *, ref: str, sha: str) -> bool:
        self._check_ref(repository, ref)
        require_sha(sha)
        return self._read_pin(repository, ref) == sha and self._commit_exists(
            repository, sha
        )

    def read_pinned_ref(self, repository: Path, *, ref: str) -> RetainedRef | None:
        self._check_ref(repository, ref)
        sha = self._read_pin(repository, ref)
        return RetainedRef(ref, sha) if sha is not None else None

    def delete_pinned_ref(self, repository: Path, *, ref: str, sha: str) -> None:
        if not self.verify_ref(repository, ref=ref, sha=sha):
            raise ValueError("retention pin missing or changed; retained")
        self._git.run(repository, ["update-ref", "--no-deref", "-d", ref, sha])

    def retained_refs(self, repository: Path) -> tuple[RetainedRef, ...]:
        result = self._git.run(
            repository,
            ["for-each-ref", "--format=%(refname) %(objectname)", *_PREFIXES],
        )
        return tuple(
            RetainedRef(*line.split(" ", 1)) for line in result.stdout.splitlines()
        )

    def linked_worktrees(self, repository: Path) -> tuple[Path, ...]:
        result = self._git.run(repository, ["worktree", "list", "--porcelain", "-z"])
        paths = [
            Path(item[9:])
            for item in result.stdout.split("\0")
            if item.startswith("worktree ")
        ]
        # The first entry is the durable repository, all others are disposable.
        return tuple(paths[1:])

    def _is_ancestor(self, repository: Path, left: str, right: str, env: dict[str, str] | None = None) -> bool:
        result = self._git.run(
            repository,
            ["--no-replace-objects", "merge-base", "--is-ancestor", left, right],
            check=False,
            env=env if env is not None else {
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key not in GIT_ENV_STRIP
                },
                "GIT_GRAFT_FILE": os.devnull,
                "GIT_SHALLOW_FILE": os.devnull,
            },
        )
        if result.returncode not in (0, 1):
            raise GitError(result)
        return result.returncode == 0

    def compare_commits(
        self, repository: Path, *, left: str, right: str
    ) -> AncestryRelation:
        return self._compare_commits(repository, left, right, None)

    def _compare_commits(self, repository: Path, left: str, right: str, env: dict[str, str] | None) -> AncestryRelation:
        a, b = (
            self._commit_exists(repository, left, env),
            self._commit_exists(repository, right, env),
        )
        if not a or not b:
            return (
                AncestryRelation.BOTH_UNREACHABLE
                if not a and not b
                else AncestryRelation.LEFT_UNREACHABLE
                if not a
                else AncestryRelation.RIGHT_UNREACHABLE
            )
        if left == right:
            return AncestryRelation.EQUAL
        if self._is_ancestor(repository, left, right, env):
            return AncestryRelation.ANCESTOR
        if self._is_ancestor(repository, right, left, env):
            return AncestryRelation.DESCENDANT
        return AncestryRelation.DIVERGENT

    def resolve_push_destination(self, repository: Path, *, remote: str) -> ExactPushDestination:
        return ExactPushContextOwner(self._git, self._auth).prepare(repository, remote).destination

    def push_exact(
        self,
        repository: Path,
        *,
        remote: str,
        branch: str,
        target_sha: str,
        expected_sha: str | None,
        destination: ExactPushDestination | None = None,
    ) -> ExactPushResult:
        require_remote(remote)
        require_exact_refspec(branch, target_sha, expected_sha)
        owner = ExactPushContextOwner(self._git, self._auth)
        try:
            context = owner.prepare(repository, remote)
        except ExactPushAuthenticationError as exc:
            return ExactPushResult(ExactPushOutcome.AUTH_FAILED, str(exc))
        if destination is not None and (type(destination) is not ExactPushDestination or destination != context.destination):
            raise ValueError("configured push destination changed before publication")
        self._git.run(repository, ["check-ref-format", f"refs/heads/{branch}"], env=dict(context.environment))
        if not self._commit_exists(repository, target_sha, dict(context.environment)):
            return ExactPushResult(
                ExactPushOutcome.NOT_FAST_FORWARD, "target commit unavailable"
            )
        if expected_sha is not None and self._compare_commits(
            repository, expected_sha, target_sha, dict(context.environment)
        ) not in {AncestryRelation.EQUAL, AncestryRelation.ANCESTOR}:
            return ExactPushResult(
                ExactPushOutcome.NOT_FAST_FORWARD, "positive ancestry proof required"
            )
        owner.recheck(repository, remote, context)
        target_ref = f"refs/heads/{branch}"
        try:
            result = self._git.run(
                repository,
                [
                    "push",
                    "--porcelain",
                    "--no-follow-tags",
                    "--atomic",
                    "--recurse-submodules=no",
                    f"--force-with-lease={target_ref}:{expected_sha or ''}",
                    "--",
                    context.destination.endpoint,
                    f"{target_sha}:{target_ref}",
                ],
                env=dict(context.environment),
                check=False,
                timeout_s=300,
            )
        except (GitError, OSError, TimeoutError) as exc:
            return ExactPushResult(ExactPushOutcome.TRANSIENT, str(exc))
        detail = result.stdout + result.stderr
        if result.returncode == 0:
            return ExactPushResult(ExactPushOutcome.PUSHED)
        if "[rejected]" in detail and "stale info" in detail:
            outcome = ExactPushOutcome.LEASE_REJECTED
        elif "non-fast-forward" in detail:
            outcome = ExactPushOutcome.NOT_FAST_FORWARD
        elif any(
            word in detail.lower()
            for word in (
                "authentication failed",
                "permission denied",
                "could not read username",
            )
        ):
            outcome = ExactPushOutcome.AUTH_FAILED
        else:
            outcome = ExactPushOutcome.TRANSIENT
        return ExactPushResult(outcome, detail)


def require_exact_refspec(branch: str, target: str, expected: str | None) -> None:
    require_sha(target)
    if expected is not None:
        require_sha(expected)
    if type(branch) is not str or not branch or branch.startswith(("-", "refs/")):
        raise ValueError("branch must be a short branch name")
