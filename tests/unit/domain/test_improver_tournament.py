"""The improver tournament's rules: key, anonymizer, grades, scores, ranking (#8001)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from issue_orchestrator.contracts.improver_tournament import AnswerKey, AnswerKeyItem
from issue_orchestrator.domain.improver_tournament import (
    GradesRejected,
    anonymize,
    arm_means,
    parse_sealed_key,
    rank,
    read_grades,
    score,
)

SEALED = """# Improver A/B grading key (written 2026-10-04 03:40, BEFORE any arm's results)

Grade each item per arm.

## Stalls / mechanics
1. (3) **A maintainer decision doesn't reach the review or the rework.** The operator's #364 ruling
   was posted as an issue comment.
2. (2) **Rework-cycle budget burned by sibling merges.** #385 is at cycle 9 of 10.

## Design / judgement (the class the improver has never found)
9. (3) **Approval is the REMOVAL of a label (`proposed-tech-lead`).** Anything that strips labels
   approves.

## Not expected (outside io / the operator's environment); credit only if raised
- ESLint ran out of memory on io's state dir.

Max score = 8
"""
T0 = datetime(2026, 10, 4, 3, 40, tzinfo=UTC)
#: Outputs (label -> their finding ids) that name no finding.
NO_FINDINGS_1: dict[str, frozenset[str]] = {"S10": frozenset()}
NO_FINDINGS_2: dict[str, frozenset[str]] = {"S10": frozenset(), "S11": frozenset()}


def _key() -> AnswerKey:
    return parse_sealed_key(SEALED, snapshot_id="20261004", added_at=T0, added_by="coordinator")


def test_the_sealed_key_is_read_with_its_sections_weights_and_wrapped_text() -> None:
    key = _key()

    assert [(i.id, i.weight, i.category) for i in key.items] == [("1", 3, "stall"), ("2", 2, "stall"), ("9", 3, "design")]
    assert key.items[0].title == "A maintainer decision doesn't reach the review or the rework."
    # The sealed key's own context and rubric reach the graders verbatim.
    assert key.preamble.startswith("# Improver A/B grading key") and "Grade each item per arm." in key.preamble
    assert key.items[0].description == "The operator's #364 ruling was posted as an issue comment."
    assert all(i.source == "sealed_key" and i.status == "confirmed" for i in key.items)
    # "Not expected" items are not key items.
    assert key.max_score == 8


def test_a_text_without_key_items_is_refused() -> None:
    with pytest.raises(ValueError, match="no key item"):
        parse_sealed_key("# nothing here\n", snapshot_id="x", added_at=T0, added_by="me")


def test_anonymous_labels_are_unique_seeded_and_unordered() -> None:
    ids = [f"{arm}{heat}" for arm in "ABCD" for heat in (1, 2, 3)]

    first, again, other = anonymize(ids, seed=7), anonymize(ids, seed=7), anonymize(ids, seed=8)

    assert first == again and first != other
    assert sorted(first.values()) == sorted(ids)
    assert all(label.startswith("S") and 10 <= int(label[1:]) <= 99 for label in first)
    # A grader reads the outputs in label order, which is not arm order.
    assert [first[label] for label in sorted(first)] != ids


def _grades(item_grades: dict[str, str], unsupported: int = 0) -> dict:
    return {"items": {i: {"grade": g, "why": "quote"} for i, g in item_grades.items()},
            "unsupported": unsupported, "unsupported_ids": [f"u{n}" for n in range(unsupported)],
            "extras": [{"id": "x", "summary": "real"}]}


def test_an_output_scores_its_items_weights_by_grade_less_its_unsupported_findings() -> None:
    key = _key()
    answer = json.dumps({
        "S10": _grades({"1": "full", "2": "half", "9": "miss"}),
        "S11": _grades({"1": "miss", "2": "miss", "9": "full"}, unsupported=1),
    })

    grades = read_grades(f"```json\n{answer}\n```", {"S10": frozenset(), "S11": frozenset({"u0"})}, key)

    assert score(grades["S10"], key) == 3 + 1 + 0
    assert score(grades["S11"], key) == 3 - 1


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        ("not json", "not one JSON object"),
        ("[]", "not one JSON object"),
        (json.dumps({"S10": _grades({"1": "full", "2": "full", "9": "full"})}), "outputs are"),  # a label missing
        (json.dumps({"S10": _grades({"1": "full", "2": "full"}), "S11": _grades({"1": "full", "2": "full", "9": "full"})}), "the key has"),
        (json.dumps({"S10": _grades({"1": "most", "2": "full", "9": "full"}), "S11": _grades({"1": "full", "2": "full", "9": "full"})}), "S10"),
        (json.dumps({"S10": {**_grades({"1": "full", "2": "full", "9": "full"}), "unsupported": -1},
                     "S11": _grades({"1": "full", "2": "full", "9": "full"})}), "S10"),
    ],
)
def test_an_incomplete_or_malformed_grading_is_no_grade(answer: str, why: str) -> None:
    with pytest.raises(GradesRejected, match=why):
        read_grades(answer, NO_FINDINGS_2, _key())


def test_a_candidate_key_item_is_not_graded() -> None:
    candidate = AnswerKeyItem(
        id="H-8137", weight=2, title="t", description="d", category="stall", source="hindsight",
        status="candidate", added_at=T0, added_by="coordinator",
    )
    key = _key().model_copy(update={"items": (*_key().items, candidate)})

    assert [i.id for i in key.scored] == ["1", "2", "9"] and key.max_score == 8
    with pytest.raises(GradesRejected, match="the key has"):
        read_grades(json.dumps({"S10": _grades({"1": "full", "2": "full", "9": "full", "H-8137": "full"})}), NO_FINDINGS_1, key)


def test_the_2026_10_04_published_means_and_ranking_follow_from_these_rules() -> None:
    """The tournament's graders' scores per output (key max 24), as its
    files give them: C 3.8, B 1.3, A 0.2, D 0.2; C > B > A ≈ D."""
    per_grader = {
        "claude": {"A1": 0, "A2": 0, "A3": 0, "B1": 0, "B2": 2.0, "B3": 0, "C1": 2.5, "C2": 2.5, "C3": 5.5,
                   "D1": 0.5, "D2": 0, "D3": 0.5},
        "codex": {"A1": 0, "A2": 0, "A3": 1.5, "B1": 1.5, "B2": 3, "B3": 1.5, "C1": 3.5, "C2": 3.5, "C3": 5.5,
                  "D1": 0, "D2": 0, "D3": 0},
    }

    pooled = arm_means(per_grader, {label: label[0] for label in per_grader["claude"]})
    means = {arm: sum(v) / len(v) for arm, v in pooled.items()}

    assert {arm: round(m, 2) for arm, m in means.items()} == {"A": 0.25, "B": 1.33, "C": 3.83, "D": 0.17}
    assert rank(means, tie_margin=0.5) == (("C",), ("B",), ("A", "D"))


def test_ranking_groups_arms_within_the_margin_of_their_groups_best() -> None:
    assert rank({"a": 3.0, "b": 2.6, "c": 2.2, "d": 0.1}, tie_margin=0.5) == (("a", "b"), ("c",), ("d",))
    assert rank({"x": 1.0, "y": 1.0}, tie_margin=0.0) == (("x", "y"),)


def test_the_graders_read_the_keys_preamble_and_each_item_in_its_own_words() -> None:
    from issue_orchestrator.execution.improver_tournament import render_key

    sealed = SEALED.replace(
        "2. (2) **Rework-cycle budget burned by sibling merges.** #385 is at cycle 9 of 10.",
        "2. (2) **Stale rows with no label**, on #293 and #186.",
    )
    rendered = render_key(parse_sealed_key(sealed, snapshot_id="x", added_at=T0, added_by="me"))

    assert rendered.startswith("# Improver A/B grading key") and "Grade each item per arm." in rendered
    assert "- **2** (2, stall) **Stale rows with no label**, on #293 and #186." in rendered
    assert "ESLint" not in rendered


def test_one_fenced_block_after_a_sentence_is_read_and_two_are_ambiguous() -> None:
    key = _key()
    body = json.dumps({"S10": _grades({"1": "full", "2": "miss", "9": "miss"})})

    grades = read_grades(f"I read the key and the outputs; the grades follow.\n\n```json\n{body}\n```\n", NO_FINDINGS_1, key)

    assert score(grades["S10"], key) == 3
    with pytest.raises(GradesRejected, match="2 fenced block"):
        read_grades(f"first\n```json\n{body}\n```\nsecond\n```json\n{body}\n```", NO_FINDINGS_1, key)
    with pytest.raises(GradesRejected, match="0 fenced block"):
        read_grades("no grades today", NO_FINDINGS_1, key)


def test_a_grading_that_names_an_output_or_an_item_twice_is_refused() -> None:
    """``json`` keeps a repeated key's last value silently: a grader could
    grade one output twice and only the second would count."""
    key = _key()
    one = json.dumps(_grades({"1": "full", "2": "miss", "9": "miss"}))
    other = json.dumps(_grades({"1": "miss", "2": "miss", "9": "miss"}))
    twice_label = f'{{"S10": {one}, "S10": {other}}}'
    twice_item = ('{"S10": {"items": {"1": {"grade": "full", "why": "q"}, "1": {"grade": "miss", "why": "q"},'
                  ' "2": {"grade": "miss", "why": "q"}, "9": {"grade": "miss", "why": "q"}}, "unsupported": 0}}')

    with pytest.raises(GradesRejected, match=r"\['S10'\] more than once"):
        read_grades(twice_label, NO_FINDINGS_1, key)
    with pytest.raises(GradesRejected, match=r"\['1'\] more than once"):
        read_grades(twice_item, NO_FINDINGS_1, key)


@pytest.mark.parametrize(
    ("unsupported", "ids", "why"),
    [(0, ["f1"], "unsupported is 0 but unsupported_ids names 1"), (2, ["f1"], "unsupported is 2"),
     (2, ["f1", "f1"], "unsupported_ids repeat")],
)
def test_an_unsupported_count_must_match_the_findings_it_names(unsupported: int, ids: list[str], why: str) -> None:
    """A grader naming an unsupported finding but counting 0 would score it free."""
    grading = {**_grades({"1": "full", "2": "miss", "9": "miss"}), "unsupported": unsupported, "unsupported_ids": ids}

    with pytest.raises(GradesRejected, match=why):
        read_grades(json.dumps({"S10": grading}), NO_FINDINGS_1, _key())


def test_only_a_finding_the_output_has_can_be_called_unsupported() -> None:
    """A grader naming a finding the output does not have would deduct a
    point for nothing."""
    grading = {**_grades({"1": "full", "2": "miss", "9": "miss"}), "unsupported": 1, "unsupported_ids": ["ghost"]}

    with pytest.raises(GradesRejected, match=r"S10 calls unsupported \['ghost'\], which are not its findings"):
        read_grades(json.dumps({"S10": grading}), {"S10": frozenset({"f1"})}, _key())
    real = {**grading, "unsupported_ids": ["f1"]}
    assert score(read_grades(json.dumps({"S10": real}), {"S10": frozenset({"f1"})}, _key())["S10"], _key()) == 2


def test_an_outputs_findings_are_its_stall_and_design_finding_ids() -> None:
    from issue_orchestrator.domain.improver_tournament import finding_ids

    doc = {"findings": [{"id": "a"}, {"id": "b"}], "design_findings": [{"id": "d"}], "other": [{"id": "x"}]}
    assert finding_ids(json.dumps(doc)) == {"a", "b", "d"}
    assert finding_ids("not json") == frozenset() and finding_ids("[1]") == frozenset()
