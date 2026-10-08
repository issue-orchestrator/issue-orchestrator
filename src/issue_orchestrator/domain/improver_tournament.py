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
from itertools import combinations
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
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
        if isinstance(doc.get(kind), list)
        for f in doc[kind]
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
    grader and pass), the arm's mean, each grader's mean, and each of each
    grader's passes' mean."""

    output_means: tuple[float, ...]
    mean: float
    grader_means: Mapping[str, float]
    #: grader -> the arm's mean score in each of that grader's passes.
    pass_means: Mapping[str, tuple[float, ...]]
    se: float


@dataclass(frozen=True)
class NoiseComponents:
    """The tournament's noise. ``heat`` is a variance (None: no arm ran two
    heats); ``grader`` and ``pass_`` are what they add to the variance of a
    difference between two arms, averaged over every pair."""

    #: One heat's (output's) mean score about its arm's mean, pooled over every arm.
    heat: float | None
    #: The graders' disagreement on a difference: var over graders of each one's difference / graders.
    grader: float
    #: The passes' noise on a difference: within-grader var over passes / (passes x graders).
    pass_: float


@dataclass(frozen=True)
class PooledScores:
    """Every arm's pooled score and the noise it was measured under.

    Two arms are told apart only when both hold:

    * **Heats.** Heats are the arms' independent samples: an exact
      one-sided permutation test over the two arms' per-heat scores must
      reach ``ALPHA``. With two heats an arm can never pass it (the most
      extreme split has p = 1/6); three heats each, wholly separated, give
      p = 1/20.
    * **Noise band.** The gap exceeds twice the standard error of the
      difference: each arm's heats' spread on its own heat count, plus the larger of the graders'
      disagreement on the difference and its pass-to-pass noise (both
      paired: a grader's bias shared by both arms cancels). Each component
      is the larger of its estimate on the two arms and on the whole
      tournament (so it is neither zero by a pair's chance agreement nor
      diluted by quiet arms), and never below what a grading resolves.
    """

    arms: Mapping[str, PooledArm]
    graders: tuple[str, ...]
    passes: int
    #: The tournament's noise, pooled over every arm.
    noise: NoiseComponents
    #: The smallest score step a grading expresses (half credit on the lightest key item).
    resolution: float

    def difference_se(self, a: str, b: str) -> float:
        # Each arm's heats on its own count (arms may run different numbers),
        # never below the tournament's heat noise.
        heat = sum(
            max(_pooled_variance([list(self.arms[x].output_means)]) or 0.0, self.noise.heat or 0.0)
            / len(self.arms[x].output_means)
            for x in (a, b)
        )
        grader = max(_grader_term(self.arms[a], self.arms[b], self.graders), self.noise.grader)
        pass_ = max(_pass_term(self.arms[a], self.arms[b], self.graders, self.passes), self.noise.pass_)
        return math.sqrt(max(heat + max(grader, pass_), 2 * self.resolution_floor()))

    def resolution_floor(self) -> float:
        """One arm's least variance from grading precision: a grader rounds
        every output it grades the same way, so more heats do not shrink it
        (half a step each way, over the graders)."""
        return self.resolution ** 2 / (4 * len(self.graders))

    def band(self, a: str, b: str) -> float:
        """How far apart two arms' means must be to tell them apart (with the heats' test)."""
        return NOISE_BAND_SES * self.difference_se(a, b)

    def heat_p(self, a: str, b: str) -> float:
        """One-sided exact permutation p that the higher arm's heats beat the lower's by chance."""
        hi, lo = (a, b) if self.arms[a].mean >= self.arms[b].mean else (b, a)
        return _permutation_p(self.arms[hi].output_means, self.arms[lo].output_means)

    def distinguishable(self, a: str, b: str) -> bool:
        gap = abs(self.arms[a].mean - self.arms[b].mean)
        return gap > self.band(a, b) and self.heat_p(a, b) <= ALPHA


#: How many standard errors of a difference separate two arms (about 95%).
NOISE_BAND_SES = 2.0
#: The heats' permutation test's level.
ALPHA = 0.05


def pool(
    gradings: Mapping[str, Sequence[Mapping[str, float]]],
    arm_of: Mapping[str, str],
    *,
    ungraded: Mapping[str, str],
    resolution: float,
) -> PooledScores:
    """Pool complete gradings (grader -> its passes, each label -> score)
    into each arm's score with the tournament's noise. ``arm_of`` maps
    graded labels to arms; ``ungraded`` outputs (no answer) score 0 in every
    grading; ``resolution`` is the smallest score step a grading expresses.
    Every grader must have made the same number of passes.
    """
    if resolution <= 0:
        raise ValueError(f"a grading resolves some step, not {resolution}")
    labels = set(arm_of)
    passes = _passes_of(gradings, labels)
    outputs: dict[str, list[str]] = {}
    for label, arm in arm_of.items():
        outputs.setdefault(arm, []).append(label)
    for output_id, arm in ungraded.items():
        outputs.setdefault(arm, []).append(f"(none){output_id}")
    graders = tuple(sorted(gradings))
    nothing = {label: 0.0 for ls in outputs.values() for label in ls if label.startswith("(none)")}
    runs = {g: [{**r, **nothing} for r in gradings[g]] for g in graders}
    arms = {arm: _pooled_arm(arm_labels, runs) for arm, arm_labels in outputs.items()}
    scores = PooledScores(
        arms=arms, graders=graders, passes=passes, noise=_components(list(arms.values()), graders, passes),
        resolution=resolution,
    )
    return replace(scores, arms={arm: replace(a, se=_arm_se(scores, arm)) for arm, a in arms.items()})


def _components(arms: Sequence[PooledArm], graders: Sequence[str], passes: int) -> NoiseComponents:
    pairs = list(combinations(arms, 2))
    return NoiseComponents(
        heat=_pooled_variance([list(a.output_means) for a in arms]),
        grader=sum(_grader_term(a, b, graders) for a, b in pairs) / len(pairs) if pairs else 0.0,
        pass_=sum(_pass_term(a, b, graders, passes) for a, b in pairs) / len(pairs) if pairs else 0.0,
    )


def _grader_term(a: PooledArm, b: PooledArm, graders: Sequence[str]) -> float:
    """The graders' disagreement on ``a - b``: its variance over graders / graders."""
    diffs = [a.grader_means[g] - b.grader_means[g] for g in graders]
    return (_pooled_variance([diffs]) or 0.0) / len(diffs)


def _pass_term(a: PooledArm, b: PooledArm, graders: Sequence[str], passes: int) -> float:
    """``a - b``'s pass-to-pass noise: within-grader variance over passes / (passes x graders)."""
    groups = [[x - y for x, y in zip(a.pass_means[g], b.pass_means[g], strict=True)] for g in graders]
    return (_pooled_variance(groups) or 0.0) / (passes * len(graders))


def _arm_se(scores: PooledScores, arm: str) -> float:
    """For display: the standard error of one arm's mean, never below what
    its own measurements show (pairwise ``difference_se`` decides)."""
    a, g, p, noise = scores.arms[arm], len(scores.graders), scores.passes, scores.noise
    n = len(a.output_means)
    heat = max(_pooled_variance([list(a.output_means)]) or 0.0, noise.heat or 0.0)
    own_grader = (_pooled_variance([list(a.grader_means.values())]) or 0.0) / g
    own_pass = (_pooled_variance([list(a.pass_means[x]) for x in scores.graders]) or 0.0) / (p * g)
    shared = max(own_grader, own_pass, max(noise.grader, noise.pass_) / 2)  # a tournament difference's, halved
    return math.sqrt(max(heat / n + shared, scores.resolution_floor()))


def _pooled_variance(groups: Sequence[Sequence[float]]) -> float | None:
    """The within-group variance pooled over groups (None: no degree of freedom)."""
    squares, dof = 0.0, 0
    for values in groups:
        if len(values) > 1:
            m = sum(values) / len(values)
            squares += sum((v - m) ** 2 for v in values)
            dof += len(values) - 1
    return squares / dof if dof else None


def _permutation_p(high: Sequence[float], low: Sequence[float]) -> float:
    """The share of all splits of the pooled heats into groups of these sizes
    whose first group beats the second by at least the observed gap."""
    together = [*high, *low]
    observed = sum(high) / len(high) - sum(low) / len(low)
    total = sum(together)
    hits = splits = 0
    for chosen in combinations(range(len(together)), len(high)):
        first = sum(together[i] for i in chosen)
        gap = first / len(high) - (total - first) / len(low)
        hits += gap >= observed - 1e-9
        splits += 1
    return hits / splits


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


def _pooled_arm(labels: Sequence[str], runs: Mapping[str, Sequence[Mapping[str, float]]]) -> PooledArm:
    n = len(labels)
    every = [r for g in runs for r in runs[g]]
    output_means = [sum(r[label] for r in every) / len(every) for label in labels]
    pass_means = {g: tuple(sum(r[label] for label in labels) / n for r in runs[g]) for g in runs}
    return PooledArm(
        output_means=tuple(sorted(output_means)),
        mean=sum(output_means) / n,
        grader_means={g: sum(m) / len(m) for g, m in pass_means.items()},
        pass_means=pass_means,
        se=0.0,
    )


def rank(means: Mapping[str, float], *, distinguishable: Callable[[str, str], bool]) -> tuple[tuple[str, ...], ...]:
    """Best first, in tiers: a tier ends only where every arm above is told
    apart from every arm below, so ">" between tiers always holds pairwise.
    Within a tier arms are in mean order; which of them are told apart is
    the pairwise record, not the tier."""
    ordered = sorted(means, key=lambda arm: (-means[arm], arm))
    tiers: list[list[str]] = []
    start = 0
    for cut in range(1, len(ordered) + 1):
        above, below = ordered[start:cut], ordered[cut:]
        if cut == len(ordered) or all(distinguishable(x, y) for x in ordered[:cut] for y in below):
            tiers.append(above)
            start = cut
    return tuple(tuple(t) for t in tiers)


__all__ = ["ALPHA", "NOISE_BAND_SES", "GradesRejected", "NoiseComponents", "PooledArm", "PooledScores", "anonymize", "finding_ids", "parse_sealed_key", "pool", "rank", "read_grades", "score"]
