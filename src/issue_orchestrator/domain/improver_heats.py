"""Merge the accepted heats of one improver run into one findings document (#8001).

A run may send N heats (independent agent runs on the same staged inputs);
the 2026-10-04 tournament showed real variance within one arm (3.0 to 5.5 of
24). Each heat's answer is validated alone; the accepted ones are merged
here, and how many heats found each finding is kept as its support: a
finding several heats found independently is stronger evidence.

ONE identity policy, shared with the GitHub effects: two findings are the
same finding exactly when they have the same effect key (the caller's
``stall_key`` / ``design_key``, the keys their issues are deduplicated by).
Nothing looser (a shared citation, a similar summary) merges two findings.

What cannot be merged is surfaced, never silently dropped
(:class:`HeatConflict`):

* a design finding with the same key but a different claim (summary or
  proposed change) is support for neither; its claim is recorded as a
  conflict on the kept finding;
* a stall finding proposing a new exam case id another, different finding
  already proposes (a case id names one case) is recorded as a conflict on
  the finding that kept the id.

The PRIMARY heat (the accepted heat with the most findings, the earliest on
a tie) keeps its blocked-item accounts, trend and finding ids: its accounts
already name its own findings. A finding only another heat found is added;
if its id is taken, it is suffixed with its heat, and its ORIGINAL id is
kept (``original_ids``) so its effect key stays the same from run to run.
The merged document is validated again by the caller, like any answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..contracts.improver_findings import DesignFinding, Finding, ImproverFindings
from .improver_citations import normalized

_F = TypeVar("_F", Finding, DesignFinding)


@dataclass(frozen=True)
class AcceptedHeat:
    heat: int
    findings: ImproverFindings


@dataclass(frozen=True)
class HeatConflict:
    """What a heat found that could not be merged into ``finding_id``."""

    finding_id: str
    heat: int
    reason: str
    claim: str


@dataclass(frozen=True)
class MergedHeats:
    findings: ImproverFindings
    #: Each merged finding's id (stall and design) -> the heats that found it.
    support: dict[str, tuple[int, ...]]
    primary: int
    conflicts: tuple[HeatConflict, ...]
    #: A renamed finding's merged id -> its id as its heat wrote it.
    original_ids: dict[str, str]


def merge_heats(
    heats: Sequence[AcceptedHeat],
    stall_key: Callable[[Finding], str],
    design_key: Callable[[DesignFinding], str],
) -> MergedHeats:
    if not heats:
        raise ValueError("no accepted heat to merge")
    primary = max(heats, key=lambda h: (len(h.findings.findings) + len(h.findings.design_findings), -h.heat))
    merge = _Merge(primary, stall_key, design_key)
    for heat in sorted(heats, key=lambda h: h.heat):
        if heat.heat != primary.heat:
            merge.add(heat)
    return merge.result()


class _Merge:
    def __init__(
        self,
        primary: AcceptedHeat,
        stall_key: Callable[[Finding], str],
        design_key: Callable[[DesignFinding], str],
    ) -> None:
        self._primary = primary
        self._stall_key = stall_key
        self._design_key = design_key
        self._stalls = list(primary.findings.findings)
        self._designs = list(primary.findings.design_findings)
        self._taken = {f.id for f in self._stalls} | {d.id for d in self._designs}
        self._support: dict[str, list[int]] = {i: [primary.heat] for i in self._taken}
        self._by_stall_key = {stall_key(f): f.id for f in self._stalls}
        self._by_case = {c: f.id for f in self._stalls if (c := _case_id(f)) is not None}
        self._by_design_key = {design_key(d): d for d in self._designs}
        self._conflicts: list[HeatConflict] = []
        self._original_ids: dict[str, str] = {}

    def add(self, heat: AcceptedHeat) -> None:
        for finding in heat.findings.findings:
            self._add_stall(heat.heat, finding)
        for design in heat.findings.design_findings:
            self._add_design(heat.heat, design)

    def _add_stall(self, heat: int, finding: Finding) -> None:
        key = self._stall_key(finding)
        if key in self._by_stall_key:
            self._back(self._by_stall_key[key], heat)
            return
        case = _case_id(finding)
        if case is not None and case in self._by_case:
            self._conflicts.append(HeatConflict(
                finding_id=self._by_case[case], heat=heat,
                reason=f"a different finding ({finding.id}) proposes the same new exam case {case}",
                claim=finding.proposal or finding.id,
            ))
            return
        kept = self._kept(finding, heat)
        self._stalls.append(kept)
        self._by_stall_key[key] = kept.id
        if case is not None:
            self._by_case[case] = kept.id

    def _add_design(self, heat: int, design: DesignFinding) -> None:
        key = self._design_key(design)
        same = self._by_design_key.get(key)
        if same is None:
            kept = self._kept(design, heat)
            self._designs.append(kept)
            self._by_design_key[key] = kept
            return
        if _claim(same) == _claim(design):
            self._back(same.id, heat)
            return
        self._conflicts.append(HeatConflict(
            finding_id=same.id, heat=heat,
            reason="the same design finding id with a different claim",
            claim=f"{design.summary} Proposed change: {design.proposed_change}",
        ))

    def _back(self, finding_id: str, heat: int) -> None:
        if heat not in self._support[finding_id]:
            self._support[finding_id].append(heat)

    def _kept(self, finding: _F, heat: int) -> _F:
        new_id = finding.id
        suffix = 0
        while new_id in self._taken:
            suffix += 1
            new_id = f"{finding.id[:72]}-h{heat}" + (f"-{suffix}" if suffix > 1 else "")
        self._taken.add(new_id)
        self._support[new_id] = [heat]
        if new_id == finding.id:
            return finding
        self._original_ids[new_id] = finding.id
        return finding.model_copy(update={"id": new_id})

    def result(self) -> MergedHeats:
        merged = self._primary.findings.model_copy(
            update={"findings": tuple(self._stalls), "design_findings": tuple(self._designs)}
        )
        return MergedHeats(
            findings=merged,
            support={finding_id: tuple(sorted(heats)) for finding_id, heats in self._support.items()},
            primary=self._primary.heat,
            conflicts=tuple(self._conflicts),
            original_ids=dict(self._original_ids),
        )


def _case_id(finding: Finding) -> str | None:
    return finding.reproduction.case_id if finding.reproduction else None


def _claim(design: DesignFinding) -> tuple[str, str]:
    return normalized(design.summary).casefold(), normalized(design.proposed_change).casefold()


__all__ = ["AcceptedHeat", "HeatConflict", "MergedHeats", "merge_heats"]
