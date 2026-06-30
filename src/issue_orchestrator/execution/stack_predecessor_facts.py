"""Git/PR fact gathering for stack predecessors (ADR-0029).

Concrete :class:`StackPredecessorFactsProvider` that reads a stack
predecessor's branch / PR / lifecycle state through the :class:`RepositoryHost`
port and projects it into the
:class:`~issue_orchestrator.domain.dependency_gates.PredecessorFacts` the
dependency gate report consumes for the *work* gate.

Scope (bounded for #6595):

- ``branch_usable`` / ``branch_name`` come from the predecessor's open PR head
  branch — an open PR is the orchestrator's evidence of a usable base. The
  candidate PR is validated here: it must be ``open`` and its head branch must
  belong to the predecessor issue (``<number>-...``). ``get_prs_for_issue`` is a
  broad association lookup (it also matches PRs that merely mention the issue in
  their title) and the production adapter does not currently enforce its
  ``state`` filter, so this gate is defensive rather than trusting the candidate
  list — a closed or unrelated-branch PR must fail safe, never launch a
  successor on a stale or wrong base.
- ``validation_passed`` is conservative: it requires a usable branch with no
  issue-scoped ``validation-failed`` / blocking label and no PR-scoped blocking
  label. The orchestrator only publishes a PR after its validation gate passes,
  so an open, non-failed PR head is treated as validated. Binding validation to
  the exact branch-head commit via the PR status-check rollup is **#6596**'s job
  (it owns the stacked-branch / SHA machinery); until then this stays fail-safe.
- ``agent_reviewed`` requires the selected predecessor **PR** to carry the
  ``code-reviewed`` label and *not* carry ``needs-rework`` — review approval is
  PR-scoped (the completion / review-exchange paths add ``code-reviewed`` to the
  PR, not the issue), so this reads PR labels, not issue labels. There is no git
  fact for "an agent reviewed this"; ADR-0029 §3 treats review *freshness* as
  the separate ``approval_current`` fact.
- ``merged`` is left False here; a merged/closed predecessor already satisfies
  the stack edge upstream, so facts are only gathered for *unsatisfied* ones.

Every path is **fail-safe**: any missing signal, cross-repo predecessor the
local host cannot query, or transient error yields conservative all-False facts
so the dependent slice stays blocked rather than launching on an unverified
base. The just-before-launch recheck re-runs this gathering so a state change
between planning and launch cannot start stale work.
"""

import logging
from collections.abc import Mapping, Sequence

from ..control.label_manager import LabelManager
from ..domain.branch_naming import extract_issue_number_from_branch
from ..domain.dependencies import DependencyTarget
from ..domain.dependency_gates import PredecessorFacts
from ..ports.pull_request_tracker import PRInfo
from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


class GitStackPredecessorFactsProvider:
    """Gathers stack-predecessor facts from git/PR state via the repository host."""

    def __init__(
        self,
        repository_host: RepositoryHost,
        label_manager: LabelManager,
        repo: str | None = None,
    ) -> None:
        self._repo = repository_host
        self._lm = label_manager
        self._configured_repo = repo

    def gather_facts(
        self, targets: Sequence[DependencyTarget]
    ) -> Mapping[DependencyTarget, PredecessorFacts]:
        return {target: self._facts_for(target) for target in targets}

    def _facts_for(self, target: DependencyTarget) -> PredecessorFacts:
        # The local repository host can only resolve PRs/labels for its own repo.
        # A cross-repo predecessor stays conservatively blocked until #6596 adds
        # cross-repo fact gathering.
        if target.repository is not None and target.repository != self._configured_repo:
            logger.debug(
                "Stack predecessor %s is cross-repo; reporting conservative facts",
                target,
            )
            return PredecessorFacts()

        number = target.issue_number
        try:
            candidate_prs = self._repo.get_prs_for_issue(number, state="open")
            usable_pr = next(
                (pr for pr in candidate_prs if self._is_usable_base(pr, number)),
                None,
            )
            if usable_pr is None:
                # No open PR whose head branch belongs to the predecessor issue.
                return PredecessorFacts()

            # Review/rework state is PR-scoped (added to the PR); blocking and
            # validation state is issue-scoped. Read each from where it lives.
            pr_labels = usable_pr.labels
            issue_labels = self._repo.get_issue_labels(number)
        except Exception as exc:  # fail-safe: never unblock on a read error
            logger.warning(
                "Could not gather stack-predecessor facts for %s: %s", target, exc
            )
            return PredecessorFacts()

        agent_reviewed = (
            self._lm.code_reviewed in pr_labels
            and self._lm.needs_rework not in pr_labels
        )
        validation_clean = (
            self._lm.validation_failed not in issue_labels
            and not self._lm.is_blocking_any(issue_labels)
            and not self._lm.is_blocking_any(pr_labels)
        )
        return PredecessorFacts(
            branch_usable=True,
            branch_name=usable_pr.branch,
            validation_passed=validation_clean,
            agent_reviewed=agent_reviewed,
            merged=False,
        )

    def _is_usable_base(self, pr: PRInfo, predecessor_issue: int) -> bool:
        """A usable stack base is an OPEN PR whose head branch is the predecessor's.

        ``get_prs_for_issue`` associates PRs broadly (head branch *or* a title
        mention) and the production adapter does not currently honor its
        ``state`` filter, so both invariants are enforced here: a closed PR or
        one whose branch belongs to a different issue must not be treated as a
        usable base, or a successor could launch from a stale or unrelated head.
        """
        if not pr.branch:
            return False
        if pr.state.lower() != "open":
            return False
        return extract_issue_number_from_branch(pr.branch) == predecessor_issue
