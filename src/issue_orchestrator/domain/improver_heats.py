"""Merge the accepted heats of one improver run into one findings document (#8001).

A run may send N heats (independent agent runs on the same staged inputs);
the 2026-10-04 tournament showed real variance within one arm (3.0 to 5.5 of
24). Each heat's answer is validated alone; the accepted ones are merged
here, and how many heats found each finding is kept as its support: a
finding several heats found independently is stronger evidence.

* A STALL finding's identity is its effect key (what it asks for, about
  which anomalies, with which case id): two heats with the same key found
  the same thing, and it is kept once. Two that propose the same NEW exam
  case id are one finding too: a case id names one case.
* A DESIGN finding's identity is looser (its id is the agent's own slug):
  two of the same kind are the same finding when they share an id or cite
  the same evidence (a file line, or a toolbox answer).
* The PRIMARY heat (the accepted heat with the most findings, the earliest
  on a tie) keeps its blocked-item accounts, trend and finding ids: its
  accounts already name its own findings. A finding only another heat found
  is added; if its id is taken, it is suffixed with its heat.

The merged document is validated again by the caller, like any answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..contracts.improver_findings import DesignFinding, FileCitation, Finding, ImproverFindings


_F = TypeVar("_F", Finding, DesignFinding)


@dataclass(frozen=True)
class AcceptedHeat:
    heat: int
    findings: ImproverFindings


@dataclass(frozen=True)
class MergedHeats:
    findings: ImproverFindings
    #: Each merged finding's id (stall and design) -> the heats that found it.
    support: dict[str, tuple[int, ...]]
    primary: int


def merge_heats(heats: Sequence[AcceptedHeat], finding_key: Callable[[Finding], str]) -> MergedHeats:
    if not heats:
        raise ValueError("no accepted heat to merge")
    primary = max(heats, key=lambda h: (len(h.findings.findings) + len(h.findings.design_findings), -h.heat))
    others = [h for h in sorted(heats, key=lambda h: h.heat) if h.heat != primary.heat]
    taken = {f.id for f in primary.findings.findings} | {d.id for d in primary.findings.design_findings}
    support: dict[str, list[int]] = {i: [primary.heat] for i in taken}

    stalls = list(primary.findings.findings)
    by_key = {finding_key(f): f.id for f in stalls}
    by_case = {c: f.id for f in stalls if (c := _case_id(f)) is not None}
    designs = list(primary.findings.design_findings)
    for heat in others:
        for finding in heat.findings.findings:
            key, case = finding_key(finding), _case_id(finding)
            same = by_key.get(key) or (by_case.get(case) if case is not None else None)
            if same is not None:
                _support(support, same, heat.heat)
                continue
            kept = _unique(finding, taken, heat.heat)
            stalls.append(kept)
            by_key[key] = kept.id
            if case is not None:
                by_case[case] = kept.id
            support[kept.id] = [heat.heat]
        for design in heat.findings.design_findings:
            same = next((d for d in designs if _same_design(d, design)), None)
            if same is not None:
                _support(support, same.id, heat.heat)
                continue
            kept_design = _unique(design, taken, heat.heat)
            designs.append(kept_design)
            support[kept_design.id] = [heat.heat]
    merged = primary.findings.model_copy(update={"findings": tuple(stalls), "design_findings": tuple(designs)})
    return MergedHeats(
        findings=merged,
        support={finding_id: tuple(sorted(set(heats_seen))) for finding_id, heats_seen in support.items()},
        primary=primary.heat,
    )


def _case_id(finding: Finding) -> str | None:
    return finding.reproduction.case_id if finding.reproduction else None


def _support(support: dict[str, list[int]], finding_id: str, heat: int) -> None:
    if heat not in support[finding_id]:
        support[finding_id].append(heat)


def _unique(finding: _F, taken: set[str], heat: int) -> _F:
    new_id = finding.id
    suffix = 0
    while new_id in taken:
        suffix += 1
        new_id = f"{finding.id[:72]}-h{heat}" + (f"-{suffix}" if suffix > 1 else "")
    taken.add(new_id)
    return finding if new_id == finding.id else finding.model_copy(update={"id": new_id})


def _citations(design: DesignFinding) -> set[tuple[str, ...]]:
    return {
        ("file", c.path, str(c.line)) if isinstance(c, FileCitation) else ("tool", str(c.call))
        for c in design.evidence
    }


def _same_design(a: DesignFinding, b: DesignFinding) -> bool:
    return a.kind == b.kind and (a.id == b.id or bool(_citations(a) & _citations(b)))


__all__ = ["AcceptedHeat", "MergedHeats", "merge_heats"]
