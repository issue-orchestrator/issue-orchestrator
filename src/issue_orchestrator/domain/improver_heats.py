"""Merge the accepted heats of one improver run into one findings document (#8001).

A run may send N heats (independent agent runs on the same staged inputs);
the 2026-10-04 tournament showed real variance within one arm (3.0 to 5.5 of
24). Each heat's answer is validated alone; the accepted ones are merged
here, and how many heats found each finding is kept as its support: a
finding several heats found independently is stronger evidence.

ONE identity policy, shared with the GitHub effects (the caller's
:class:`FindingIdentity`, the owner of the effect keys):

* two findings are the same finding exactly when they have the same effect
  key (the keys their issues are deduplicated by);
* a design finding is the SAME DEFECT as another finding of any kind (#8700)
  when the identity's :func:`~.improver_defects.same_defect` says so: one
  code site and overlapping evidence. It is folded into that finding
  (:class:`SameDefect`): it files no issue of its own, its heats support the
  finding, and its whole claim is shown on the finding's issue. Only a
  direct relation to exactly one finding folds (:func:`_same_defect_folds`).

Nothing looser (a shared citation alone, a similar summary) merges two
findings.

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

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol, TypeVar

from ..contracts.improver_findings import DesignFinding, Finding, ImproverFindings
from .improver_champion import CHANGE_ID
from .improver_citations import normalized
from .improver_defects import DefectProfile, same_defect

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


class FindingIdentity(Protocol):
    """The one identity policy of findings: their effect keys, and the
    profile that says which are one defect."""

    def stall_key(self, finding: Finding) -> str: ...

    def design_key(self, design: DesignFinding) -> str: ...

    def stall_profile(self, finding: Finding) -> DefectProfile: ...

    def design_profile(self, design: DesignFinding) -> DefectProfile: ...


@dataclass(frozen=True)
class SameDefect:
    """A design finding folded into ``finding_id``, the finding about the
    same defect: ``design`` as its heat wrote it."""

    finding_id: str
    design: DesignFinding
    heats: tuple[int, ...]


@dataclass(frozen=True)
class MergedHeats:
    findings: ImproverFindings
    #: Each merged finding's id (stall and design) -> the heats that found it.
    support: dict[str, tuple[int, ...]]
    primary: int
    conflicts: tuple[HeatConflict, ...]
    #: A renamed finding's merged id -> its id as its heat wrote it.
    original_ids: dict[str, str]
    #: The design findings folded into another finding about the same defect.
    same_defects: tuple[SameDefect, ...]


def merge_heats(heats: Sequence[AcceptedHeat], identity: FindingIdentity) -> MergedHeats:
    if not heats:
        raise ValueError("no accepted heat to merge")
    primary = max(heats, key=lambda h: (len(h.findings.findings) + len(h.findings.design_findings), -h.heat))
    merge = _Merge(primary, identity)
    for heat in sorted(heats, key=lambda h: h.heat):
        if heat.heat != primary.heat:
            merge.add(heat)
    return merge.result()


class _Merge:
    def __init__(self, primary: AcceptedHeat, identity: FindingIdentity) -> None:
        self._primary = primary
        self._identity = identity
        stall_key, design_key = identity.stall_key, identity.design_key
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
        change = heat.findings.improver_change
        if change is not None and change != self._primary.findings.improver_change:
            # Only the primary heat's change is carried: its motivating
            # findings stand unrenamed. Another heat's is shown, not dropped.
            self._conflicts.append(HeatConflict(
                finding_id=CHANGE_ID, heat=heat.heat,
                reason="a change to the improver from a heat other than the primary is not carried",
                claim=change.model_dump_json(),
            ))
        for finding in heat.findings.findings:
            self._add_stall(heat.heat, finding)
        for design in heat.findings.design_findings:
            self._add_design(heat.heat, design)

    def _add_stall(self, heat: int, finding: Finding) -> None:
        key = self._identity.stall_key(finding)
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
        key = self._identity.design_key(design)
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
            claim=f"{design.summary} Impact: {design.impact} Proposed change: {design.proposed_change}"
            f" Owner: {design.owner}. Evidence: "
            + "; ".join(c.model_dump_json() for c in design.evidence),
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
        folds = _same_defect_folds(self._identity, self._stalls, self._designs)
        folded_heats = {design_id: tuple(sorted(self._support.pop(design_id))) for design_id in folds}
        for design_id, into in folds.items():
            for heat in folded_heats[design_id]:
                self._back(into, heat)
        kept = tuple(d for d in self._designs if d.id not in folds)
        change = self._primary.findings.improver_change
        if change is not None and set(change.motivated_by) & set(folds):
            # A folded design finding's issue is its defect's finding's issue.
            motives = dict.fromkeys(folds.get(i, i) for i in change.motivated_by)
            change = change.model_copy(update={"motivated_by": tuple(motives)})
        merged = self._primary.findings.model_copy(
            update={"findings": tuple(self._stalls), "design_findings": kept, "improver_change": change}
        )
        support = {finding_id: tuple(sorted(heats)) for finding_id, heats in self._support.items()}
        return MergedHeats(
            findings=merged,
            support=support,
            primary=self._primary.heat,
            # A folded design finding's conflicts are shown on its defect's issue.
            conflicts=tuple(
                replace(c, finding_id=folds[c.finding_id]) if c.finding_id in folds else c
                for c in self._conflicts
            ),
            original_ids={new: old for new, old in self._original_ids.items() if new not in folds},
            same_defects=tuple(
                SameDefect(
                    finding_id=into,
                    design=d.model_copy(update={"id": self._original_ids.get(d.id, d.id)}),
                    heats=folded_heats[d.id],
                )
                for d in self._designs
                if (into := folds.get(d.id)) is not None
            ),
        )


def _same_defect_folds(
    identity: FindingIdentity, stalls: Sequence[Finding], designs: Sequence[DesignFinding]
) -> dict[str, str]:
    """Each design finding folded into another finding about its defect:
    design id -> the id it is folded into.

    Only a DIRECT relation folds (r1 F2): one design finding related to
    another only through a third may be about another defect. In order
    (the primary heat's first), a design finding folds into a finding that
    files its own issue (a stall finding, or an earlier design finding that
    folded into nothing) only if it is related to that finding AND to every
    design finding already folded into it (r3 F1, r4 F1: so no order of
    the findings, and no finding both are related to, joins two that are
    not related). A stall finding that qualifies comes first: with exactly
    one, it folds there; with none, into the one design finding that
    qualifies (r5 F1: a stall finding it cannot join does not keep it from
    the design finding it can); with two of either, into none, since which
    one carries it is not known."""
    #: Each finding that files its own issue, with those folded into it.
    stall_groups: list[list[DefectProfile]] = [[identity.stall_profile(f)] for f in stalls]
    design_groups: list[list[DefectProfile]] = []
    folds: dict[str, str] = {}
    for design in designs:
        profile = identity.design_profile(design)
        eligible = _qualifying(profile, stall_groups) or _qualifying(profile, design_groups)
        if len(eligible) == 1:
            folds[design.id] = eligible[0][0].finding_id
            eligible[0].append(profile)
        else:
            design_groups.append([profile])
    return folds


def _qualifying(profile: DefectProfile, groups: list[list[DefectProfile]]) -> list[list[DefectProfile]]:
    """The groups whose every member is the same defect as ``profile``."""
    return [g for g in groups if all(same_defect(profile, m) for m in g)]


def _case_id(finding: Finding) -> str | None:
    return finding.reproduction.case_id if finding.reproduction else None


def _claim(design: DesignFinding) -> tuple[object, ...]:
    """All a design finding claims (r2 F3): two heats back one claim only
    if they say the same thing, cite the same evidence and name the same
    owner; anything else is a conflict, shown, never dropped."""
    return (
        *(normalized(t).casefold() for t in (design.summary, design.impact, design.proposed_change)),
        design.owner,
        frozenset(c.model_dump_json() for c in design.evidence),
    )


__all__ = ["AcceptedHeat", "FindingIdentity", "HeatConflict", "MergedHeats", "SameDefect", "merge_heats"]
