"""The live e2e suite's one owner of closing PRs and deleting branches (#9523).

On 2026-10-14 the suite's cleanup closed 17 real PRs and deleted their
branches: it chose PRs by workflow labels (``needs-code-review``,
``code-reviewed``) that real work carries. Whether a PR or branch may be
removed is now decided in one place, here, from the evidence a typed scope
carries, and every close and delete in ``tests/e2e`` goes through
:class:`E2EArtifactCleanup`:

- :class:`RunOwnedScope` -- a run's own teardown. A PR is admitted only if it
  carries an e2e marker AND belongs to an issue the run recorded creating
  (:data:`RUN_ISSUES`, or an ``E2EFlow``'s own issues).
- :class:`ExamRunScope` -- an exam run's teardown. A PR is admitted when its
  head is a branch the run recorded at creation, or its body links an issue
  the run owns; a branch only when the run recorded it at creation.
- :class:`IdleRepositoryScope` -- an operator's explicit statement that no
  run is live against the test repository (``python -m tests.e2e.reconcile``).
  No run can prove another has ended, so the suite never removes an unknown
  run's PRs on its own.

A branch is never deleted because its name matches. Deleting one closes every
open PR it heads or is the base of, so each of those is read completely and
uncached and must be admitted too, before anything is written. The owner
exists only for the validated test repository (``get_test_repo``) and only in
a process the live gate allows, so destruction stays closed however the suite
was reached.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from issue_orchestrator.control.review_scope import issues_of_pr
from issue_orchestrator.domain.pr_issue_reference import linked_issue_numbers
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

    def get_open_prs_for_branch_complete(self, branch: str) -> list[PRInfo]: ...

    def list_open_prs_complete(self) -> list[PRInfo]: ...

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
        return tuple(sorted(self.markers)) if self.issue_numbers else ()

    @property
    def listing_state(self) -> str:
        return "open"

    def admits_pr(self, pr: PRInfo, repo: str) -> bool:
        return any(is_e2e_marker(label) for label in pr.labels) and bool(
            issues_of_pr(pr, repo_slug=repo) & self.issue_numbers
        )

    def admits_branch(self, branch: str) -> bool:
        return False


class RunIssues:
    """The issues this run (one pytest session) created, with their labels.

    Every e2e issue creation records here (``fixtures.data_factory``); the
    session's teardown removes the PRs of exactly these issues. An issue
    missing from it only leaves its PR behind for the operator's
    reconciliation; it can never put another run's PR in scope.
    """

    def __init__(self) -> None:
        self._labels: dict[int, frozenset[str]] = {}

    def record(self, issue_number: int, labels: Iterable[str]) -> None:
        self._labels[issue_number] = frozenset(labels)

    def scope(self) -> RunOwnedScope:
        labels = {label for labels in self._labels.values() for label in labels}
        return RunOwnedScope.of(self._labels, labels)


#: This process's run: the e2e suite runs one pytest session per process.
RUN_ISSUES = RunIssues()


@dataclass(frozen=True)
class ExamRunScope:
    """An exam run's artifacts.

    ``run_branches`` were recorded the moment the run created them: the ones
    the harness pushed, and the ones the run's own engine clone created
    (``EngineCheckout.remove`` reads them before the clone is gone).
    ``issue_numbers`` are the issues the run created or labelled with its own
    unique run label (``RunIdentity.label``); a PR whose body links one of
    them is the run's, even without e2e labels (the engine files issues and
    publishes recovery PRs itself). A branch NAMED after an owned issue proves
    nothing: anyone can push ``7-anything``.
    """

    issue_numbers: frozenset[int]
    run_branches: frozenset[str] = frozenset()

    def admits_pr(self, pr: PRInfo, repo: str) -> bool:
        return pr.branch in self.run_branches or bool(
            linked_issue_numbers(pr.body or "", repo_slug=repo) & self.issue_numbers
        )

    def admits_branch(self, branch: str) -> bool:
        return branch in self.run_branches


@dataclass(frozen=True)
class IdleRepositoryScope:
    """An operator's statement that no e2e run is live against ``repo``.

    Only then is every PR carrying the static e2e artifact label a leftover:
    nothing a run could observe proves another run has ended (#9523), so only
    ``python -m tests.e2e.reconcile`` builds this, from an explicit
    confirmation naming the repository.
    """

    repo: str

    @property
    def listing_labels(self) -> tuple[str, ...]:
        return (E2E_ARTIFACT_LABEL,)

    @property
    def listing_state(self) -> str:
        return "all"

    def admits_pr(self, pr: PRInfo, repo: str) -> bool:
        return repo.casefold() == self.repo.casefold() and E2E_ARTIFACT_LABEL in pr.labels

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
        not admit it (or a PR its branch deletion would close); nothing is
        written then.
        """
        pr = self._github.get_pr(pr_number)
        if pr is None:
            raise CleanupRefusedError(
                f"PR #{pr_number} cannot be read, so nothing proves it is an e2e artifact"
            )
        self._remove(scope, pr)

    def delete_branch(self, scope: CleanupScope, branch: str) -> None:
        """Delete ``branch``, if the scope recorded it, closing the open PRs it heads."""
        if not scope.admits_branch(branch):
            raise CleanupRefusedError(f"branch {branch} is not in this cleanup's scope")
        if not self._github.branch_exists(branch):
            return
        affected = self._open_prs_closed_by_deleting(branch)
        for pr in affected:
            self._admit(scope, pr)
        for pr in affected:
            if pr.branch == branch:
                self._github.close_pr(pr.number)
        self._github.delete_branch(branch)

    def sweep(self, scope: SweptScope) -> int:
        """Remove every admitted PR the scope's listing finds; the number closed.

        Best effort per PR, so one failure does not strand the rest; a PR the
        scope does not admit, or whose branch deletion would close one it does
        not admit, is never written to.
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
                except Exception as exc:
                    logger.warning("[E2E CLEANUP] Retained PR #%d: %s", pr.number, exc)
        return closed

    def open_prs_with_label(self, label: str) -> list[PRInfo]:
        """Read-only: the open PRs carrying ``label`` (for reporting leftovers)."""
        return self._github.get_prs_with_label(label, state="open")

    def _admit(self, scope: CleanupScope, pr: PRInfo) -> None:
        if not scope.admits_pr(pr, self.repo):
            raise CleanupRefusedError(
                f"PR #{pr.number} ({pr.branch}, labels {sorted(pr.labels)}) is not "
                "an e2e artifact this cleanup's scope owns"
            )

    def _open_prs_closed_by_deleting(self, branch: str) -> list[PRInfo]:
        """Every open PR whose head or base is ``branch``: deleting it closes them.

        Both reads are complete and uncached, and raise rather than return a
        partial answer, so a PR the cache or a capped page would hide still
        counts. The complete listing carries no labels, so a PR based on
        ``branch`` is read whole before it is judged.
        """
        affected = {pr.number: pr for pr in self._github.get_open_prs_for_branch_complete(branch)}
        for listed in self._github.list_open_prs_complete():
            if listed.base_branch != branch or listed.number in affected:
                continue
            pr = self._github.get_pr(listed.number)
            if pr is None:
                raise CleanupRefusedError(
                    f"PR #{listed.number} is based on {branch} but cannot be read"
                )
            affected[pr.number] = pr
        return [pr for pr in affected.values() if pr.state.lower() == "open"]

    def _remove(self, scope: CleanupScope, pr: PRInfo) -> int:
        """Close ``pr`` if open and delete its branch; 1 if it was closed.

        Everything is admitted before anything is written: ``pr``, and every
        other open PR deleting its branch would close.
        """
        self._admit(scope, pr)
        branch = pr.branch if pr.branch and self._github.branch_exists(pr.branch) else None
        if branch is not None:
            for affected in self._open_prs_closed_by_deleting(branch):
                if affected.number != pr.number:
                    self._admit(scope, affected)
        was_open = pr.state.lower() == "open"
        if was_open:
            self._github.close_pr(pr.number)
        if branch is not None:
            self._github.delete_branch(branch)
        logger.info("[E2E CLEANUP] Removed PR #%d and branch %s", pr.number, pr.branch)
        return int(was_open)
