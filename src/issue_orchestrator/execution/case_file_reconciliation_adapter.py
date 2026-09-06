"""Execution adapter for the existing case-file reconciliation owner."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ..control.action_results import ActionResult
from ..control.actions import Action
from ..domain.tech_lead_findings import PatternEvidence


class CaseFileReconciliationAdapter:
    """Binds the reconciliation's execution boundary to a live orchestrator.

    The three effects the run cannot perform itself: read the pattern ledger,
    ask whether one named issue is still open, and apply actions through the
    ordinary ``ActionApplier``. No policy lives here.

    Each is injected as the narrow callable it is (the same shape
    :class:`~...control.tech_lead_case_file_owner.PatternCaseFileOwner` takes
    ``add_comment`` in) rather than as the whole orchestrator, so this binding
    is exercisable without one — and so the entrypoint depends on behavior, not
    on how the composition root happens to nest its dependencies.
    """

    def __init__(
        self,
        *,
        list_pattern_evidence: Callable[[], Sequence["PatternEvidence"]],
        get_issue_state: Callable[[int], str | None],
        apply_all: Callable[[Sequence["Action"]], Sequence["ActionResult"]],
    ) -> None:
        self._list_pattern_evidence = list_pattern_evidence
        self._get_issue_state = get_issue_state
        self._apply_all = apply_all

    def pattern_ledger(self) -> Mapping[str, "PatternEvidence"]:
        from ..control.tech_lead_case_files import build_pattern_ledger

        return build_pattern_ledger(self._list_pattern_evidence())

    def issue_is_open(self, issue_number: int) -> bool:
        # A missing issue (deleted, transferred) is NOT open: never close what
        # cannot be confirmed open. Non-404 read failures raise out of the
        # adapter, which halts the run rather than guessing.
        return self._get_issue_state(issue_number) == "open"

    def apply(self, actions: Sequence["Action"]) -> Sequence["ActionResult"]:
        if not actions:
            return ()
        return self._apply_all(list(actions))


