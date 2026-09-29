"""Apply the improver's accepted findings on GitHub, recording each receipt (#7490).

The IO half of :mod:`..control.improver_effects`: for every accepted run that
still owes effects, oldest first, it asks the policy what each finding needs
(:func:`~..control.improver_effects.plan_effect`) and does it through the
repository-host port, recording the receipt on the run as it goes.

Open issues are listed once per application, so a finding an open issue
already carries (filed by an earlier run, or by this one before a crash lost
its receipt) is commented there, never filed twice. A comment is posted only
if its marker is not already on the issue. A rate limit stops the batch with
the rest pending; any other failure stops it too, kept as the receipt's
``error`` until an application succeeds.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol

from ..contracts.improver_findings import Finding
from ..contracts.improver_run import EffectReceipt, EffectStatus, ImproverRunRecord, RunOutcome
from ..control.improver_effects import (
    IMPROVER_LABEL,
    CommentImproverEvidence,
    plan_effect,
    title_token,
)
from ..ports.engine_audit import OpenIssueLabels
from ..ports.improver import ImproverRunStore
from ..ports.repository_host import host_rate_limit_of


class ImproverIssueHost(Protocol):
    """The repository-host writes and read the effects need."""

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None, milestone: int | None = None
    ) -> dict[str, Any] | None: ...

    def add_comment(self, issue_or_pr_number: int, body: str) -> str: ...

    def issue_comment_marker_present(self, issue_number: int, marker: str) -> bool: ...

    def list_open_issue_labels_complete(self) -> Sequence[OpenIssueLabels]: ...

    def find_issue_by_marker(
        self, *, title: str, marker: str, authoritative: bool = False
    ) -> int | None: ...

    def find_open_issue_by_marker(self, *, marker: str) -> int | None: ...


class ImproverEffects:
    """Applies the effects owed to ONE repository, ``outputs_repo``, whose host
    ``host`` is; a run filed for another repository is never touched here."""

    def __init__(
        self,
        *,
        store: ImproverRunStore,
        host: ImproverIssueHost,
        outputs_repo: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._host = host
        self._outputs_repo = outputs_repo
        self._clock = clock

    @property
    def outputs_repo(self) -> str:
        return self._outputs_repo

    def owing_runs(self) -> tuple[str, ...]:
        """Accepted runs that still owe this repository an effect, oldest first."""
        return tuple(run.run_id for run in self._owing())

    def apply_pending(self) -> tuple[ImproverRunRecord, ...]:
        """Apply every accepted run's pending effects, oldest run first.

        Never raises for GitHub: a rate limit stops the batch (the rest stay
        pending) and any other failure is recorded on its receipt, which
        stays pending, before the batch stops. Returns the runs it updated.
        """
        owing = self._owing()
        if not owing:
            return ()
        try:
            open_issues = {i.number: i for i in self._host.list_open_issue_labels_complete()}
        except Exception as error:
            first = owing[0]
            return (self._stopped(first, first.effects.index(first.pending_effects[0]), error),)
        updated = []
        for run in owing:
            run, stopped = self._apply_run(run, open_issues)
            self._store.record(run)
            updated.append(run)
            if stopped:
                break
        return tuple(updated)

    def _owing(self) -> list[ImproverRunRecord]:
        return [
            run for run in reversed(self._store.runs())
            if run.outcome is RunOutcome.ACCEPTED
            and run.pending_effects
            and run.outputs_repo == self._outputs_repo
        ]

    def _apply_run(
        self, run: ImproverRunRecord, open_issues: dict[int, OpenIssueLabels]
    ) -> tuple[ImproverRunRecord, bool]:
        findings = {f.id: f for f in self._store.accepted_findings(run).findings}
        for index, receipt in enumerate(run.effects):
            if receipt.status is not EffectStatus.PENDING:
                continue
            def persist(intent: EffectReceipt, *, at: int = index) -> None:
                nonlocal run
                run = run.model_copy(update={"effects": _replaced(run.effects, at, intent)})
                self._store.record(run)

            try:
                applied = self._apply(run, findings[receipt.finding_id], receipt, open_issues, persist)
            except Exception as error:
                return self._stopped(run, index, error), True
            run = run.model_copy(update={"effects": _replaced(run.effects, index, applied)})
        return run, False

    def _stopped(self, run: ImproverRunRecord, index: int, error: Exception) -> ImproverRunRecord:
        """``run`` with its ``index``-th effect left pending, saying why, recorded.

        A rate limit only defers; anything else is kept as the receipt's
        ``error`` until an application succeeds.
        """
        limit = host_rate_limit_of(error)
        update = (
            {"detail": f"rate limited until {limit.resets_at.isoformat()}", "error": None}
            if limit is not None
            else {"detail": "", "error": f"{type(error).__name__}: {error}"}
        )
        receipt = run.effects[index].model_copy(update=update)
        stopped = run.model_copy(update={"effects": _replaced(run.effects, index, receipt)})
        self._store.record(stopped)
        return stopped

    def _apply(
        self,
        run: ImproverRunRecord,
        finding: Finding,
        receipt: EffectReceipt,
        open_issues: dict[int, OpenIssueLabels],
        persist: Callable[[EffectReceipt], None],
    ) -> EffectReceipt:
        command = plan_effect(run, finding, receipt.key, open_issues)
        if isinstance(command, CommentImproverEvidence):
            if not self._host.issue_comment_marker_present(command.issue_number, command.marker):
                self._host.add_comment(command.issue_number, command.body)
            target = command.issue_number
            return receipt.model_copy(
                update={
                    "status": EffectStatus.COMMENTED, "issue_number": target,
                    "at": self._clock(), "detail": "", "error": None,
                }
            )
        if receipt.create_attempted_at is not None:
            # An earlier POST's result was lost. Prove by the body marker,
            # over every issue open or closed, whether it filed one.
            number = self._host.find_issue_by_marker(
                title=command.title, marker=command.marker, authoritative=True
            )
        else:
            # Another run may have filed the same finding a moment ago (the
            # open listing need not show it yet), or an operator may have
            # retitled or relabelled it: an uncached, complete read of every
            # open issue's body settles it before any POST.
            number = self._host.find_open_issue_by_marker(marker=command.marker)
            if number is not None:
                return self._apply(run, finding, receipt, {**open_issues, number: _carrying(number, receipt.key)}, persist)
        if number is None:
            persist(receipt.model_copy(update={"create_attempted_at": self._clock()}))
            created = self._host.create_issue(
                title=command.title, body=command.body, labels=list(command.labels)
            )
            if not created or not isinstance(created.get("number"), int):
                raise RuntimeError(f"creating the improver issue for {finding.id} returned no issue number")
            number = created["number"]
        open_issues[number] = OpenIssueLabels(number=number, title=command.title, labels=command.labels)
        return receipt.model_copy(
            update={
                "status": EffectStatus.FILED, "issue_number": number,
                "at": self._clock(), "detail": "", "error": None,
                "create_attempted_at": receipt.create_attempted_at or self._clock(),
            }
        )


def _carrying(number: int, key: str) -> OpenIssueLabels:
    """An open issue known (by its body marker) to carry the finding ``key``."""
    return OpenIssueLabels(number=number, title=title_token(key), labels=(IMPROVER_LABEL,))


def _replaced(
    effects: tuple[EffectReceipt, ...], index: int, receipt: EffectReceipt
) -> tuple[EffectReceipt, ...]:
    return (*effects[:index], receipt, *effects[index + 1 :])


__all__ = ["ImproverEffects", "ImproverIssueHost"]
