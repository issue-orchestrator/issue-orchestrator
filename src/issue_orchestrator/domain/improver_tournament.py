"""The improver tournament's rules, pure (#8001).

* :func:`parse_sealed_key` reads a sealed grading key (the 2026-10-04 one's
  markdown: ``N. (W) **title** text`` under a "Stalls" or "Design" section)
  into an :class:`~..contracts.improver_tournament.AnswerKey`, keeping its
  preamble (what was audited, how to grade) for the graders.
* :func:`anonymize` labels the outputs ``S10``..``S99`` in a seeded random
  order, so graders cannot tell which arm wrote which.
* :func:`read_grades` accepts a grader's answer only if it grades every
  label on exactly the key's scored items; anything else is no grade.
* :func:`score`, :func:`pool` and :func:`rank`: an output earns each
  item's weight times its grade's credit (full 1, half 1/2, miss 0), less
  1 per unsupported finding; graded k times by each grader (passes
  averaged per grader first), an arm's score is the mean over its outputs
  and graders. Two arms are told apart only when their means differ by
  more than twice the standard error of the difference, from the graders'
  disagreement on it and the heats' spread (the noise band); arms within
  it share a place.

The rules reproduce the 2026-10-04 tournament's published means (C 3.8, B
1.3, A 0.2, D 0.2) from its graders' files.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from ..contracts.improver_tournament import (
    GRADE_CREDIT,
    AnswerKey,
    AnswerKeyItem,
    OutputGrades,
)

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


def finding_ids(output: str) -> frozenset[str]:
    """The ids of an output's findings (stall and design): what a grader may
    call unsupported. An output that is not a findings document has none."""
    try:
        doc = json.loads(output)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(doc, dict):
        return frozenset()
    return frozenset(
        f["id"]
        for kind in ("findings", "design_findings")
        for f in doc.get(kind, [])
        if isinstance(f, dict) and isinstance(f.get("id"), str)
    )


def read_grades(answer: str, outputs: Mapping[str, AbstractSet[str]], key: AnswerKey) -> dict[str, OutputGrades]:
    """A grader's answer, if it grades every output (label -> its finding
    ids) on exactly the key's scored items, and calls unsupported only
    findings that output has."""
    raw = _one_json_object(answer)
    if not isinstance(raw, dict):
        raise GradesRejected("not one JSON object")
    expected_labels, expected_items = set(outputs), {i.id for i in key.scored}
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
        unknown = sorted(set(grade.unsupported_ids) - set(outputs[label]))
        if unknown:
            raise GradesRejected(f"{label} calls unsupported {unknown}, which are not its findings")
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
        return json.loads(candidates[0], object_pairs_hook=_no_repeated_keys)
    except json.JSONDecodeError as error:
        raise GradesRejected(f"not one JSON object: {error}") from error


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object, refused if it names one key twice (``json`` would keep
    the last silently: two grades for one output, or for one item)."""
    keys = [k for k, _ in pairs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise GradesRejected(f"names {repeated} more than once")
    return dict(pairs)


def score(grades: OutputGrades, key: AnswerKey) -> float:
    weights = {i.id: i.weight for i in key.scored}
    return sum(weights[item] * GRADE_CREDIT[g.grade] for item, g in grades.items.items()) - grades.unsupported


@dataclass(frozen=True)
class PooledArm:
    """One arm over every grading: each output's mean score (over every
    grader, each grader's passes averaged first), their mean, each
    grader's mean, and the mean's standard error (for display; arms are
    compared pairwise, see :meth:`PooledScores.difference_se`)."""

    output_means: tuple[float, ...]
    mean: float
    grader_means: Mapping[str, float]
    #: The spread of the output means about the arm's mean (None: one output).
    heat_variance: float | None
    se: float


@dataclass(frozen=True)
class PooledScores:
    """Every arm's pooled score, and the noise it was measured under.

    The independent units are the graders and the heats (outputs); a
    grader's repeated passes over the same output measure only its own
    pass-to-pass noise (``pass_sd``) and are averaged before anything else,
    so repeating a grader's fixed opinion never adds certainty.
    """

    arms: Mapping[str, PooledArm]
    graders: tuple[str, ...]
    passes: int
    #: One pass's spread about its grader's mean for that output (None: one pass).
    pass_sd: float | None

    def difference_se(self, a: str, b: str) -> float:
        """The standard error of ``mean(a) - mean(b)``: graders' disagreement
        on that difference, plus the heats' spread (never below what pass
        noise alone would give)."""
        g = len(self.graders)
        diffs = [self.arms[a].grader_means[x] - self.arms[b].grader_means[x] for x in self.graders]
        mean_diff = sum(diffs) / g
        grader_term = sum((d - mean_diff) ** 2 for d in diffs) / (g - 1) / g if g > 1 else 0.0
        heat_term = sum(
            (self.arms[x].heat_variance or 0.0) / len(self.arms[x].output_means) for x in (a, b)
        )
        pass_floor = (self.pass_sd or 0.0) ** 2 / (self.passes * g) * sum(
            1 / len(self.arms[x].output_means) for x in (a, b)
        )
        return math.sqrt(grader_term + max(heat_term, pass_floor))

    def band(self, a: str, b: str) -> float:
        """How far apart two arms' means must be to tell them apart."""
        return NOISE_BAND_SES * self.difference_se(a, b)

    def distinguishable(self, a: str, b: str) -> bool:
        return abs(self.arms[a].mean - self.arms[b].mean) > self.band(a, b)


#: How many standard errors of a difference separate two arms (about 95%).
NOISE_BAND_SES = 2.0


def pool(
    gradings: Mapping[str, Sequence[Mapping[str, float]]],
    arm_of: Mapping[str, str],
    *,
    ungraded: Mapping[str, str],
) -> PooledScores:
    """Pool complete gradings (grader -> its passes, each label -> score)
    into each arm's score with its noise. ``arm_of`` maps graded labels to
    arms; ``ungraded`` outputs (no answer to grade) score 0 in every grading.
    Every grader must have made the same number of passes.
    """
    labels = set(arm_of)
    passes = _passes_of(gradings, labels)
    # Each grader's view of each output: its passes averaged.
    views = {g: {label: sum(r[label] for r in runs) / passes for label in labels} for g, runs in gradings.items()}
    if passes > 1 and labels:
        squares = sum((r[label] - views[g][label]) ** 2 for g, runs in gradings.items() for r in runs for label in labels)
        pass_var: float | None = squares / (len(gradings) * len(labels) * (passes - 1))
    else:
        pass_var = None
    outputs: dict[str, list[str]] = {}
    for label, arm in arm_of.items():
        outputs.setdefault(arm, []).append(label)
    for output_id, arm in ungraded.items():
        outputs.setdefault(arm, []).append(f"(none){output_id}")

    graders = tuple(sorted(gradings))
    # An output with no answer scores 0 in every grader's view.
    full = {g: {**views[g], **{label: 0.0 for arm_labels in outputs.values() for label in arm_labels
                               if label.startswith("(none)")}} for g in graders}
    floor_per_output = (pass_var or 0.0) / (passes * len(graders))
    arms = {arm: _pooled_arm(arm_labels, full, floor_per_output) for arm, arm_labels in outputs.items()}
    return PooledScores(
        arms=arms, graders=graders, passes=passes, pass_sd=None if pass_var is None else math.sqrt(pass_var)
    )


def _passes_of(gradings: Mapping[str, Sequence[Mapping[str, float]]], labels: set[str]) -> int:
    """How many passes every grader made, if each pass scored every output."""
    if not gradings:
        raise ValueError("no grading to pool")
    pass_counts = {len(p) for p in gradings.values()}
    if len(pass_counts) != 1 or 0 in pass_counts:
        raise ValueError(f"every grader makes the same number of passes, not {sorted(pass_counts)}")
    for grader, runs in gradings.items():
        for n, by_label in enumerate(runs, 1):
            if set(by_label) != labels:
                raise ValueError(f"grading {grader}#{n} scores {sorted(by_label)}, not every output {sorted(labels)}")
    return pass_counts.pop()


def _pooled_arm(
    labels: Sequence[str], views: Mapping[str, Mapping[str, float]], floor_per_output: float
) -> PooledArm:
    graders = sorted(views)
    n = len(labels)
    output_means = [sum(views[g][label] for g in graders) / len(graders) for label in labels]
    mean = sum(output_means) / n
    heat_var = sum((v - mean) ** 2 for v in output_means) / (n - 1) if n > 1 else None
    grader_means = {g: sum(views[g][label] for label in labels) / n for g in graders}
    gm = list(grader_means.values())
    grader_term = sum((x - mean) ** 2 for x in gm) / (len(gm) - 1) / len(gm) if len(gm) > 1 else 0.0
    return PooledArm(
        output_means=tuple(sorted(output_means)), mean=mean, grader_means=grader_means, heat_variance=heat_var,
        se=math.sqrt(grader_term + max((heat_var or 0.0) / n, floor_per_output / n)),
    )


def rank(means: Mapping[str, float], *, distinguishable: Callable[[str, str], bool]) -> tuple[tuple[str, ...], ...]:
    """Best first, in groups of arms that are pairwise indistinguishable: an
    arm joins the current group only if no member is distinguishable from
    it. (Between groups, ">" orders by mean; whether two arms in different
    groups are told apart is the pairwise record, not the grouping.)"""
    ordered = sorted(means, key=lambda arm: (-means[arm], arm))
    groups: list[list[str]] = []
    for arm in ordered:
        if groups and not any(distinguishable(member, arm) for member in groups[-1]):
            groups[-1].append(arm)
        else:
            groups.append([arm])
    return tuple(tuple(g) for g in groups)


__all__ = ["NOISE_BAND_SES", "GradesRejected", "PooledArm", "PooledScores", "anonymize", "finding_ids", "parse_sealed_key", "pool", "rank", "read_grades", "score"]
