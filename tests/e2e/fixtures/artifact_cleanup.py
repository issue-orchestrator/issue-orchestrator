"""The live e2e suite's one owner of closing PRs and deleting branches (#9523).

On 2026-10-14 the suite's cleanup closed 17 real PRs and deleted their
branches: it chose PRs by workflow labels (``needs-code-review``,
``code-reviewed``) that real work carries. Whether a PR or branch may be
removed is now decided in one place, here, from the evidence a typed scope
carries, and every close and delete in ``tests/e2e`` goes through
:class:`E2EArtifactCleanup`:

- :class:`RunOwnedScope` -- a run's own teardown. A PR is admitted only if it
  carries an e2e marker AND belongs to an issue the run created.
- :class:`ExamRunScope` -- an exam run's teardown. Its issues are the ones it
  created or labelled with its own unique run label; its branches are the
  ones its harness pushed, or the engine named after one of those issues.
- :class:`StaleArtifactScope` -- reconciliation of earlier runs' leftovers. A
  PR is admitted only if it carries the static e2e artifact label AND was
  created before any run still going could have started, so a concurrent
  run's live PRs are never touched.

Outside the exam, a branch is deleted only as the head of an admitted PR,
never because its name matches. The owner exists only for the validated test
repository (``get_test_repo``) and only in a process the live gate allows, so
destruction stays closed however the suite was reached.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from issue_orchestrator.control.review_scope import issues_of_pr
from issue_orchestrator.domain.branch_naming import extract_issue_number_from_branch
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.live_e2e_gate import LiveE2EGate

from .github_client import _github_adapter
from .github_utils import get_test_repo

logger = logging.getLogger(__name__)

#: The static label every e2e artifact carries (issues, and PRs through
#: ``e2e_pr_labels``). Specific on purpose: real work never carries it.
E2E_ARTIFACT_LABEL = "io-e2e-test-data"
#: Per-test and per-run e2e labels (``e2e_label``, run labels).
E2E_TEST_LABEL_PREFIX = "io:e2e:"
E2E_RUN_LABEL_PREFIX = "io:e2e-run-"

#: Longer than any live run lasts. A PR younger than this may belong to a run
#: still going, so stale reconciliation leaves it to that run's own teardown.
MAX_LIVE_RUN_AGE = timedelta(hours=12)


def is_e2e_marker(label: str) -> bool:
    """Whether ``label`` marks an e2e artifact. Workflow labels never do."""
    return (
        label == E2E_ARTIFACT_LABEL
        or label.startswith(E2E_TEST_LABEL_PREFIX)
        or label.startswith(E2E_RUN_LABEL_PREFIX)
    )


class CleanupRefusedError(RuntimeError):
    """A close or delete the scope's evidence does not admit."""


class CleanupGitHub(Protocol):
    """The GitHub surface the owner reads and writes (``GitHubAdapter``)."""

    def get_pr(self, pr_number: int) -> PRInfo | None: ...

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]: ...

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]: ...

    def close_pr(self, pr_number: int) -> None: ...

    def delete_branch(self, branch: str) -> None: ...

    def branch_exists(self, branch: str) -> bool: ...


class CleanupScope(Protocol):
    """Evidence of what one cleanup may remove."""

    def admits_pr(self, pr: PRInfo, repo: str) -> bool: ...

    def admits_branch(self, branch: str) -> bool: ...


class SweptScope(CleanupScope, Protocol):
    """A scope whose candidates are found by listing PRs by label."""

    @property
    def listing_labels(self) -> tuple[str, ...]: ...

    @property
    def listing_state(self) -> str: ...


@dataclass(frozen=True)
class RunOwnedScope:
    """A run's own artifacts: the issues it created and the e2e markers they carry."""

    issue_numbers: frozenset[int]
    markers: frozenset[str]

    @classmethod
    def of(cls, issue_numbers: Iterable[int], labels: Iterable[str]) -> RunOwnedScope:
        """The scope of issues created with ``labels``; only e2e markers count."""
        return cls(
            issue_numbers=frozenset(issue_numbers),
            markers=frozenset(label for label in labels if is_e2e_marker(label)),
        )

    @property
    def listing_labels(self) -> tuple[str, ...]:
        return tuple(sorted(self.markers))

    @property
    def listing_state(self) -> str:
        return "open"

    def admits_pr(self, pr: PRInfo, repo: str) -> bool:
        return any(is_e2e_marker(label) for label in pr.labels) and bool(
            issues_of_pr(pr, repo_slug=repo) & self.issue_numbers
        )

    def admits_branch(self, branch: str) -> bool:
        return False


@dataclass(frozen=True)
class ExamRunScope:
    """An exam run's artifacts.

    ``issue_numbers`` are the issues the run created or labelled with its own
    unique run label (``RunIdentity.label``); the engine files some itself and
    may publish recovery PRs without e2e labels, so ownership comes from the
    issue, not from a label on the PR. ``pushed_branches`` are the branches the
    harness itself pushed.
    """

    issue_numbers: frozenset[int]
    pushed_branches: frozenset[str] = frozenset()

    def admits_pr(self, pr: PRInfo, repo: str) -> bool:
        return pr.branch in self.pushed_branches or bool(
            issues_of_pr(pr, repo_slug=repo) & self.issue_numbers
        )

    def admits_branch(self, branch: str) -> bool:
        return (
            branch in self.pushed_branches
            or extract_issue_number_from_branch(branch) in self.issue_numbers
        )


@dataclass(frozen=True)
class StaleArtifactScope:
    """Earlier runs' leftovers: PRs carrying the static e2e artifact label,
    created before ``created_before``."""

    created_before: datetime

    @classmethod
    def as_of(cls, now: datetime) -> StaleArtifactScope:
        """Leftovers no run that may still be going at ``now`` can own."""
        return cls(created_before=now - MAX_LIVE_RUN_AGE)

    @property
    def listing_labels(self) -> tuple[str, ...]:
        return (E2E_ARTIFACT_LABEL,)

    @property
    def listing_state(self) -> str:
        return "all"

    def admits_pr(self, pr: PRInfo, repo: str) -> bool:
        if E2E_ARTIFACT_LABEL not in pr.labels or not pr.created_at:
            return False
        try:
            created = datetime.fromisoformat(pr.created_at)
        except ValueError:
            return False
        return created.tzinfo is not None and created < self.created_before

    def admits_branch(self, branch: str) -> bool:
        return False


#: Proof that :meth:`E2EArtifactCleanup.for_test_repo` validated the target.
_VALIDATED = object()


class E2EArtifactCleanup:
    """Closes PRs and deletes branches in the e2e test repository, and only
    what a scope admits. Construct it with :meth:`for_test_repo`."""

    def __init__(self, repo: str, github: CleanupGitHub, *, validated: object) -> None:
        if validated is not _VALIDATED:
            raise TypeError("construct E2EArtifactCleanup with for_test_repo(), which validates")
        self.repo = repo
        self._github = github

    @classmethod
    def for_test_repo(
        cls,
        repo: str,
        *,
        github: CleanupGitHub | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> E2EArtifactCleanup:
        """The owner for ``repo``, which must be the validated test repository.

        Refuses outside a run the live gate allows (an agent session, no
        ``IO_E2E=1``) and for any repository but ``get_test_repo()``, which in
        turn refuses an unset ``E2E_TEST_REPO`` and this checkout's origin.
        """
        refusal = LiveE2EGate.from_environ(environ).suite_refusal()
        if refusal is not None:
            raise CleanupRefusedError(refusal)
        target = get_test_repo()
        if repo.casefold() != target.casefold():
            raise CleanupRefusedError(
                f"{repo} is not the validated e2e test repository ({target}); "
                "e2e cleanup never touches another repository"
            )
        return cls(
            target,
            github if github is not None else _github_adapter(target),
            validated=_VALIDATED,
        )

    def close_pr(self, scope: CleanupScope, pr_number: int) -> None:
        """Close PR ``pr_number`` (if open) and delete its branch.

        Raises CleanupRefusedError when the PR cannot be read or the scope does
        not admit it; nothing is written then.
        """
        pr = self._github.get_pr(pr_number)
        if pr is None:
            raise CleanupRefusedError(
                f"PR #{pr_number} cannot be read, so nothing proves it is an e2e artifact"
            )
        self._remove(scope, pr)

    def delete_branch(self, scope: CleanupScope, branch: str) -> None:
        """Close the open PRs of ``branch`` and delete it, if the scope names it."""
        if not scope.admits_branch(branch):
            raise CleanupRefusedError(f"branch {branch} is not in this cleanup's scope")
        open_prs = [
            pr for pr in self._github.get_prs_for_branch(branch, state="open")
            if pr.state.lower() == "open"
        ]
        for pr in open_prs:
            self._admit(scope, pr)
        for pr in open_prs:
            self._github.close_pr(pr.number)
        self._delete_branch_if_present(branch)

    def sweep(self, scope: SweptScope) -> int:
        """Remove every admitted PR the scope's listing finds; the number closed.

        Best effort per PR, so one failure does not strand the rest; PRs the
        scope does not admit are never written to.
        """
        seen: set[int] = set()
        closed = 0
        for label in scope.listing_labels:
            try:
                prs = self._github.get_prs_with_label(label, state=scope.listing_state)
            except Exception:
                logger.warning("[E2E CLEANUP] Failed listing PRs for label '%s'", label)
                continue
            for pr in prs:
                if pr.number in seen or not scope.admits_pr(pr, self.repo):
                    continue
                seen.add(pr.number)
                try:
                    closed += self._remove(scope, pr)
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed removing PR #%d", pr.number)
        return closed

    def _admit(self, scope: CleanupScope, pr: PRInfo) -> None:
        if not scope.admits_pr(pr, self.repo):
            raise CleanupRefusedError(
                f"PR #{pr.number} ({pr.branch}, labels {sorted(pr.labels)}) is not "
                "an e2e artifact this cleanup's scope owns"
            )

    def _remove(self, scope: CleanupScope, pr: PRInfo) -> int:
        """Close ``pr`` if open and delete its branch; 1 if it was closed.

        Deleting a branch also closes every other open PR whose head it is, so
        each of those must be admitted too, before anything is written.
        """
        self._admit(scope, pr)
        if pr.branch:
            for head in self._github.get_prs_for_branch(pr.branch, state="open"):
                if head.number != pr.number and head.state.lower() == "open":
                    self._admit(scope, head)
        was_open = pr.state.lower() == "open"
        if was_open:
            self._github.close_pr(pr.number)
        if pr.branch:
            self._delete_branch_if_present(pr.branch)
        logger.info("[E2E CLEANUP] Removed PR #%d and branch %s", pr.number, pr.branch)
        return int(was_open)

    def _delete_branch_if_present(self, branch: str) -> None:
        if self._github.branch_exists(branch):
            self._github.delete_branch(branch)


def utc_now() -> datetime:
    """Now, for :meth:`StaleArtifactScope.as_of`."""
    return datetime.now(UTC)
