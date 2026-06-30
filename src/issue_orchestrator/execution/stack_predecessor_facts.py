"""Git/PR fact gathering for stack predecessors (ADR-0029).

Concrete :class:`StackPredecessorFactsProvider` that reads a stack
predecessor's branch / PR / lifecycle state through the :class:`RepositoryHost`
port and projects it into the
:class:`~issue_orchestrator.domain.dependency_gates.PredecessorFacts` the
dependency gate report consumes for the *work* gate.

Scope (bounded for #6595):

- ``branch_usable`` / ``branch_name`` come from the predecessor's open PR head
  branch — an open PR is the orchestrator's evidence of a usable base.
- ``validation_passed`` is conservative: it requires a usable branch with no
  ``validation-failed`` / blocking label. The orchestrator only publishes a PR
  after its validation gate passes, so an open, non-failed PR head is treated as
  validated. Binding validation to the exact branch-head commit via the PR
  status-check rollup is **#6596**'s job (it owns the stacked-branch / SHA
  machinery); until then this stays fail-safe.
- ``agent_reviewed`` requires the predecessor to carry the ``code-reviewed``
  label — the orchestrator's authoritative record that an agent reviewed the
  current head (there is no git fact for "an agent reviewed this"; ADR-0029 §3
  treats review *freshness* as the separate ``approval_current`` fact).
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
from ..domain.dependencies import DependencyTarget
from ..domain.dependency_gates import PredecessorFacts
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
            open_prs = self._repo.get_prs_for_issue(number, state="open")
            usable_pr = next((pr for pr in open_prs if pr.branch), None)
            if usable_pr is None:
                # No usable open branch to base on yet.
                return PredecessorFacts()

            labels = self._repo.get_issue_labels(number)
        except Exception as exc:  # fail-safe: never unblock on a read error
            logger.warning(
                "Could not gather stack-predecessor facts for %s: %s", target, exc
            )
            return PredecessorFacts()

        validation_clean = (
            self._lm.validation_failed not in labels
            and not self._lm.is_blocking_any(labels)
        )
        return PredecessorFacts(
            branch_usable=True,
            branch_name=usable_pr.branch,
            validation_passed=validation_clean,
            agent_reviewed=self._lm.code_reviewed in labels,
            merged=False,
        )
