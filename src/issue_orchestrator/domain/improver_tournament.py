"""The improver tournament's rules, pure (#8001).

* :func:`parse_sealed_key` reads a sealed grading key (the 2026-10-04 one's
  markdown: ``N. (W) **title** text`` under a "Stalls" or "Design" section)
  into an :class:`~..contracts.improver_tournament.AnswerKey`, keeping its
  preamble (what was audited, how to grade) for the graders.
* :func:`anonymize` labels the outputs ``S10``..``S99`` in a seeded random
  order, so graders cannot tell which arm wrote which.
* :func:`read_grades` accepts a grader's answer only if it grades every
  label on exactly the key's scored items; anything else is no grade.
* :func:`score` and :func:`rank`: an output earns each item's weight times
  its grade's credit (full 1, half 1/2, miss 0), less 1 per unsupported
  finding; an arm's score is the mean over its outputs and the graders;
  arms within ``tie_margin`` of a group's best share its place.

The rules reproduce the 2026-10-04 tournament's published means (C 3.8, B
1.3, A 0.2, D 0.2) from its graders' files.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from pydantic import ValidationError

from ..contracts.improver_tournament import GRADE_CREDIT, AnswerKey, AnswerKeyItem, OutputGrades

_ITEM = re.compile(r"^(?P<id>\d+)\. \((?P<weight>[123])\) \*\*(?P<title>.+?)\*\*\s*(?P<rest>.*)$")
_FENCED = re.compile(r"\A```(?:json)?\n(?P<body>.*)\n```\Z", re.DOTALL)
#: A fenced JSON block anywhere in a message.
_FENCED_BLOCK = re.compile(r"```(?:json)?\n(?P<body>.*?)\n```", re.DOTALL)


class GradesRejected(ValueError):
    """A grader's answer that is not a complete grading of the outputs."""


def parse_sealed_key(markdown: str, *, snapshot_id: str, added_at: datetime, added_by: str) -> AnswerKey:
    items: list[AnswerKeyItem] = []
    category: str | None = None
    current: dict[str, str] | None = None
    preamble: list[str] = []
    in_preamble = True

    def close() -> None:
        if current is not None and category is not None:
            items.append(AnswerKeyItem(
                id=current["id"], weight=int(current["weight"]), title=current["title"],
                description=" ".join(current["text"].split()), category=category,  # type: ignore[arg-type]
                source="sealed_key", status="confirmed", added_at=added_at, added_by=added_by,
            ))

    for line in markdown.splitlines():
        if line.startswith("## "):
            close()
            current = None
            in_preamble = False
            heading = line[3:].lower()
            category = "stall" if heading.startswith("stalls") else "design" if heading.startswith("design") else None
            continue
        if in_preamble:
            preamble.append(line)
            continue
        match = _ITEM.match(line)
        if match and category is not None:
            close()
            current = {**match.groupdict(), "text": match["rest"]}
        elif current is not None and line.startswith("   "):
            current["text"] += " " + line.strip()
    close()
    if not items:
        raise ValueError("no key item found: expected 'N. (W) **title** text' under a Stalls or Design section")
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"key item ids repeat: {ids}")
    return AnswerKey(snapshot_id=snapshot_id, preamble="\n".join(preamble).strip(), items=tuple(items))


def anonymize(output_ids: Sequence[str], *, seed: int) -> dict[str, str]:
    """Label -> output id, the labels drawn at random (seeded) from S10..S99."""
    if len(output_ids) > 90:
        raise ValueError("at most 90 outputs per tournament")
    if len(set(output_ids)) != len(output_ids):
        raise ValueError("output ids repeat")
    labels = random.Random(seed).sample([f"S{n}" for n in range(10, 100)], len(output_ids))
    return dict(zip(labels, output_ids, strict=True))


def read_grades(answer: str, labels: Iterable[str], key: AnswerKey) -> dict[str, OutputGrades]:
    """A grader's answer, if it grades every label on exactly the key's scored items."""
    raw = _one_json_object(answer)
    if not isinstance(raw, dict):
        raise GradesRejected("not one JSON object")
    expected_labels, expected_items = set(labels), {i.id for i in key.scored}
    if set(raw) != expected_labels:
        raise GradesRejected(f"grades {sorted(raw)} but the outputs are {sorted(expected_labels)}")
    grades: dict[str, OutputGrades] = {}
    for label, value in raw.items():
        try:
            grade = OutputGrades.model_validate(value)
        except ValidationError as error:
            raise GradesRejected(f"{label}: {error.errors()[0]['msg']} at {error.errors()[0]['loc']}") from error
        if set(grade.items) != expected_items:
            raise GradesRejected(f"{label} grades items {sorted(grade.items)}, the key has {sorted(expected_items)}")
        grades[label] = grade
    return grades


def _one_json_object(answer: str) -> object:
    """The answer's JSON: the whole message, the one fenced block it is, or
    the ONE fenced block in it (a sentence before it is tolerated; two
    blocks are ambiguous and refused)."""
    text = answer.strip()
    fenced = _FENCED.match(text)
    candidates = [fenced["body"]] if fenced else [text]
    if not fenced and not text.startswith("{"):
        blocks = _FENCED_BLOCK.findall(text)
        if len(blocks) != 1:
            raise GradesRejected(f"not one JSON object: {len(blocks)} fenced block(s) beside prose")
        candidates = blocks
    try:
        return json.loads(candidates[0])
    except json.JSONDecodeError as error:
        raise GradesRejected(f"not one JSON object: {error}") from error


def score(grades: OutputGrades, key: AnswerKey) -> float:
    weights = {i.id: i.weight for i in key.scored}
    return sum(weights[item] * GRADE_CREDIT[g.grade] for item, g in grades.items.items()) - grades.unsupported


def arm_means(per_grader: Mapping[str, Mapping[str, float]], arm_of: Mapping[str, str]) -> dict[str, list[float]]:
    """Each arm's scores, over its outputs and every grader: ``per_grader`` is
    grader -> label -> score, ``arm_of`` label -> arm."""
    scores: dict[str, list[float]] = {}
    for by_label in per_grader.values():
        for label, value in by_label.items():
            scores.setdefault(arm_of[label], []).append(value)
    return scores


def rank(means: Mapping[str, float], *, tie_margin: float) -> tuple[tuple[str, ...], ...]:
    """Best first; an arm within ``tie_margin`` of its group's best joins it."""
    ordered = sorted(means, key=lambda arm: (-means[arm], arm))
    groups: list[list[str]] = []
    for arm in ordered:
        if groups and means[groups[-1][0]] - means[arm] <= tie_margin:
            groups[-1].append(arm)
        else:
            groups.append([arm])
    return tuple(tuple(g) for g in groups)


__all__ = ["GradesRejected", "anonymize", "arm_means", "parse_sealed_key", "rank", "read_grades", "score"]
