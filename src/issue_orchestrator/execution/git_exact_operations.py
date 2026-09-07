"""Immutable pins and explicit compare-and-set pushes over the Git port."""

import re
import os

from ..adapters.git.git_cli import GIT_ENV_STRIP
from pathlib import Path

from ..domain.exact_git import (
    ExactPushDestination,
    ExactPushOutcome,
    ExactPushResult,
    RefPinOutcome,
    RetainedRef,
)
from ..domain.validated_work import require_sha
from ..domain.validated_work_store import AncestryRelation
from ..ports.git import Git, GitError
from .git_push_operations import GitAuthEnvProvider, prepare_git_auth_env

_PREFIXES = ("refs/issue-orchestrator/validated/", "refs/issue-orchestrator/observed/")


class GitExactOperations:
    def __init__(self, git: Git, auth: GitAuthEnvProvider | None) -> None:
        self._git = git
        self._auth = auth

    def _check_ref(self, repository: Path, ref: str) -> None:
        if not ref.startswith(_PREFIXES):
            raise ValueError("ref must belong to validated-work retention")
        self._git.run(repository, ["check-ref-format", ref])

    def _commit_exists(self, repository: Path, sha: str) -> bool:
        require_sha(sha)
        result = self._git.run(
            repository, ["--no-replace-objects", "cat-file", "-t", sha], check=False
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

    def _is_ancestor(self, repository: Path, left: str, right: str) -> bool:
        result = self._git.run(
            repository,
            ["--no-replace-objects", "merge-base", "--is-ancestor", left, right],
            check=False,
            env={
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
        a, b = (
            self._commit_exists(repository, left),
            self._commit_exists(repository, right),
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
        if self._is_ancestor(repository, left, right):
            return AncestryRelation.ANCESTOR
        if self._is_ancestor(repository, right, left):
            return AncestryRelation.DESCENDANT
        return AncestryRelation.DIVERGENT

    def _validate_push(
        self,
        repository: Path,
        remote: str,
        branch: str,
        target: str,
        expected: str | None,
        destination: ExactPushDestination | None,
    ) -> str:
        require_sha(target)
        if expected is not None:
            require_sha(expected)
        if type(branch) is not str or not branch or branch.startswith(("-", "refs/")):
            raise ValueError("branch must be a short branch name")
        self._git.run(repository, ["check-ref-format", f"refs/heads/{branch}"])
        if destination is None:
            return self._configured_push_destination(repository, remote=remote).endpoint
        current = self.resolve_push_destination(repository, remote=remote)
        if type(destination) is not ExactPushDestination or destination != current:
            raise ValueError("configured push destination changed before publication")
        return destination.endpoint

    def resolve_push_destination(
        self, repository: Path, *, remote: str
    ) -> ExactPushDestination:
        """Fail closed on rewriting; preserve existing configuration and hooks.

        The destination is checked again immediately before submission. This
        boundary does not lock arbitrary external writers of local/global Git
        configuration: hostile mutation after that final check is outside the
        publication threat model. No configuration is overridden to claim a
        stronger guarantee.
        """
        rewrites = self._git.run(
            repository,
            ["config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"],
            check=False,
        )
        if rewrites.returncode == 0:
            raise ValueError(
                "destination-bound publication does not support Git URL rewriting"
            )
        if rewrites.returncode != 1:
            raise GitError(rewrites)
        return self._configured_push_destination(repository, remote=remote)

    def _configured_push_destination(
        self, repository: Path, *, remote: str
    ) -> ExactPushDestination:
        # Accept configured remote names only: no options, URLs, paths or helpers.
        if (
            type(remote) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote) is None
        ):
            raise ValueError("remote must be a configured remote name")
        urls = self._git.run(
            repository, ["remote", "get-url", "--push", "--all", remote]
        ).stdout.splitlines()
        if len(urls) != 1 or not urls[0]:
            raise ValueError("exact push requires one configured push endpoint")
        endpoint = urls[0]
        # A relative local path must not be reinterpreted as another remote name.
        if ":" not in endpoint or endpoint.startswith(("/", "./", "../")):
            endpoint = str((repository / endpoint).resolve())
        return ExactPushDestination(endpoint)

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
        endpoint = self._validate_push(
            repository, remote, branch, target_sha, expected_sha, destination
        )
        if not self._commit_exists(repository, target_sha):
            return ExactPushResult(
                ExactPushOutcome.NOT_FAST_FORWARD, "target commit unavailable"
            )
        if expected_sha is not None and self.compare_commits(
            repository, left=expected_sha, right=target_sha
        ) not in {AncestryRelation.EQUAL, AncestryRelation.ANCESTOR}:
            return ExactPushResult(
                ExactPushOutcome.NOT_FAST_FORWARD, "positive ancestry proof required"
            )
        try:
            env = prepare_git_auth_env(self._auth, remote=remote)
        except Exception as exc:
            return ExactPushResult(ExactPushOutcome.AUTH_FAILED, str(exc))
        target_ref = f"refs/heads/{branch}"
        try:
            result = self._git.run(
                repository,
                [
                    "-c",
                    "push.followTags=false",
                    "push",
                    "--porcelain",
                    "--atomic",
                    "--recurse-submodules=no",
                    f"--force-with-lease={target_ref}:{expected_sha or ''}",
                    "--",
                    endpoint,
                    f"{target_sha}:{target_ref}",
                ],
                env=env,
                check=False,
                timeout_s=300,
            )
        except (GitError, OSError, TimeoutError) as exc:
            return ExactPushResult(ExactPushOutcome.TRANSIENT, str(exc))
        if result.returncode == 0:
            return ExactPushResult(ExactPushOutcome.PUSHED)
        detail = result.stdout + result.stderr
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
