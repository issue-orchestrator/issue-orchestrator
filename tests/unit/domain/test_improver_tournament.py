"""The improver tournament's rules: key, anonymizer, grades, scores, ranking (#8001)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from issue_orchestrator.contracts.improver_tournament import AnswerKey, AnswerKeyItem
from issue_orchestrator.domain.improver_tournament import (
    GradesRejected,
    anonymize,
    PooledScores,
    pool,
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


THE_10_04_GRADINGS = {
    "claude": {"A1": 0, "A2": 0, "A3": 0, "B1": 0, "B2": 2.0, "B3": 0, "C1": 2.5, "C2": 2.5, "C3": 5.5,
               "D1": 0.5, "D2": 0, "D3": 0.5},
    "codex": {"A1": 0, "A2": 0, "A3": 1.5, "B1": 1.5, "B2": 3, "B3": 1.5, "C1": 3.5, "C2": 3.5, "C3": 5.5,
              "D1": 0, "D2": 0, "D3": 0},
}


STEP = 0.5  # half credit on a weight-1 item


def _ten_four() -> PooledScores:
    return pool({g: [scores] for g, scores in THE_10_04_GRADINGS.items()},
                {label: label[0] for label in THE_10_04_GRADINGS["claude"]}, ungraded={}, resolution=STEP)


def test_the_2026_10_04_published_means_follow_from_these_rules_and_only_c_stands_out() -> None:
    """The tournament's two gradings per output (key max 24) give its
    published means; within the noise they show, only C is told apart."""
    pooled = _ten_four()

    assert {arm: round(a.mean, 2) for arm, a in pooled.arms.items()} == {"A": 0.25, "B": 1.33, "C": 3.83, "D": 0.17}
    # Heats: output means' spread within each arm, pooled (8 degrees of freedom);
    # graders: their disagreement on each pair's difference, averaged over the 6 pairs; one pass.
    assert pooled.noise.heat == pytest.approx(6.625 / 8)
    assert pooled.noise.grader == pytest.approx(0.2350, abs=1e-3)
    assert pooled.noise.pass_ == 0.0
    # C over B: a 2.5 gap against a 2.25 band, and C's three heats all beat B's (p = 1/20).
    assert pooled.band("C", "B") == pytest.approx(2.254, abs=1e-3) and pooled.heat_p("C", "B") == 0.05
    means = {arm: a.mean for arm, a in pooled.arms.items()}
    assert rank(means, distinguishable=pooled.distinguishable) == (("C",), ("B", "A", "D"))
    assert all(pooled.distinguishable("C", arm) for arm in "ABD")
    assert not any(pooled.distinguishable(x, y) for x, y in (("B", "A"), ("B", "D"), ("A", "D")))


def test_repeating_a_graders_opinion_never_makes_a_difference_certain() -> None:
    """Claude's three passes give A's outputs 1, Codex's three give them 0;
    both give B 0. The 0.5 gap is the graders' disagreement, not evidence:
    passes repeat one opinion, they are not more graders."""
    claude = [{"a1": 1.0, "a2": 1.0, "b1": 0.0, "b2": 0.0}] * 3
    codex = [{"a1": 0.0, "a2": 0.0, "b1": 0.0, "b2": 0.0}] * 3

    pooled = pool({"claude": claude, "codex": codex}, {"a1": "A", "a2": "A", "b1": "B", "b2": "B"},
                  ungraded={}, resolution=STEP)

    assert (pooled.arms["A"].mean, pooled.arms["B"].mean) == (0.5, 0.0)
    assert pooled.noise.grader == pytest.approx(0.25) and pooled.noise.pass_ == 0.0
    assert pooled.band("A", "B") == pytest.approx(1.0)  # (1 - 0)^2 / 2 graders' variance / 2
    assert not pooled.distinguishable("A", "B")


def test_a_difference_every_grader_sees_in_every_heat_is_told_apart_given_three_heats() -> None:
    def runs(n: int) -> dict[str, list[dict[str, float]]]:
        one = {**{f"a{i}": 2.0 + 0.5 * (i % 2) for i in range(n)}, **{f"b{i}": 0.5 * (i % 2) for i in range(n)}}
        other = {k: v + 0.25 for k, v in one.items()}
        return {"claude": [one, other], "codex": [other, one]}

    def arms(n: int) -> dict[str, str]:
        return {**{f"a{i}": "A" for i in range(n)}, **{f"b{i}": "B" for i in range(n)}}

    two = pool(runs(2), arms(2), ungraded={}, resolution=STEP)
    three = pool(runs(3), arms(3), ungraded={}, resolution=STEP)

    # A 2-point gap far beyond the band either way; but two heats an arm
    # cannot be told from heat luck (p = 1/6), three wholly separated can (1/20).
    assert two.band("A", "B") < 1.0 and two.heat_p("A", "B") == pytest.approx(1 / 6)
    assert not two.distinguishable("A", "B")
    assert three.heat_p("A", "B") == pytest.approx(1 / 20) and three.distinguishable("A", "B")


def test_noise_is_pooled_over_every_arm() -> None:
    """Y's heats agree and Z ran once, but X's heats disagree: the
    tournament's heats vary, so Y and Z are not certain either."""
    both = [{"x1": 4.0, "x2": 0.0, "y1": 1.0, "y2": 2.0}, {"x1": 4.0, "x2": 0.0, "y1": 2.0, "y2": 1.0}]

    pooled = pool({"g": both, "h": both}, {"x1": "X", "x2": "X", "y1": "Y", "y2": "Y"},
                  ungraded={"z1": "Z"}, resolution=STEP)

    assert pooled.noise.heat == pytest.approx(4.0)  # X: 8 on 1 degree of freedom, Y: 0 on 1
    assert pooled.arms["X"].output_means == (0.0, 4.0) and pooled.arms["Y"].output_means == (1.5, 1.5)
    # X on its own heats (8 / 2); Y's agree, so the tournament's heat noise (4 / 2).
    assert pooled.difference_se("X", "Y") == pytest.approx((8.0 / 2 + 4.0 / 2) ** 0.5)
    assert not pooled.distinguishable("X", "Y") and not pooled.distinguishable("Y", "Z")


def test_heats_that_move_together_in_a_pass_are_one_observation_of_pass_noise() -> None:
    """Both graders give A's two heats 2 in their first pass and 0 after;
    B and C score 0. Pass noise is measured on each pass's arm mean, so A's
    heats moving together count once, not twice."""
    runs = [{"a1": 2.0, "a2": 2.0, "b1": 0.0, "b2": 0.0, "c1": 0.0, "c2": 0.0},
            {"a1": 0.0, "a2": 0.0, "b1": 0.0, "b2": 0.0, "c1": 0.0, "c2": 0.0},
            {"a1": 0.0, "a2": 0.0, "b1": 0.0, "b2": 0.0, "c1": 0.0, "c2": 0.0}]
    arm_of = {"a1": "A", "a2": "A", "b1": "B", "b2": "B", "c1": "C", "c2": "C"}

    pooled = pool({"claude": runs, "codex": runs}, arm_of, ungraded={}, resolution=STEP)

    assert pooled.arms["A"].mean == pytest.approx(2 / 3)
    # Per pass, A - B is 2, 0, 0 for each grader: variance 4/3, over 3 passes x 2 graders.
    assert pooled.band("A", "B") == pytest.approx(2 * (4 / 3 / 6) ** 0.5)
    assert not pooled.distinguishable("A", "B")


def test_quiet_arms_never_shrink_a_pairs_noise() -> None:
    """Adding arms that always score 0 changes neither A nor B, nor whether they are told apart."""
    loud = [{"a1": 2.0, "a2": 2.0, "a3": 2.0, "b1": 0.0, "b2": 0.0, "b3": 0.0},
            {"a1": 0.0, "a2": 0.0, "a3": 0.0, "b1": 0.0, "b2": 0.0, "b3": 0.0},
            {"a1": 0.0, "a2": 0.0, "a3": 0.0, "b1": 0.0, "b2": 0.0, "b3": 0.0}]
    arm_of = {**{f"a{i}": "A" for i in (1, 2, 3)}, **{f"b{i}": "B" for i in (1, 2, 3)}}
    quiet = {f"{arm.lower()}{i}": arm for arm in "QRS" for i in (1, 2, 3)}

    alone = pool({"claude": loud, "codex": loud}, arm_of, ungraded={}, resolution=STEP)
    crowded = pool({g: [{**r, **{label: 0.0 for label in quiet}} for r in loud] for g in ("claude", "codex")},
                   {**arm_of, **quiet}, ungraded={}, resolution=STEP)

    assert crowded.band("A", "B") >= alone.band("A", "B")
    assert crowded.distinguishable("A", "B") == alone.distinguishable("A", "B") is False


def test_no_variation_seen_is_not_certainty() -> None:
    """Six identical gradings: what bounds the band is the grading's own
    resolution, and with one heat per arm, the heat's luck."""
    one_heat = [{"a1": 3.0, "b1": 0.5}] * 3
    pooled = pool({"claude": one_heat, "codex": one_heat}, {"a1": "A", "b1": "B"}, ungraded={}, resolution=STEP)
    # Far beyond the resolution band (0.5), but each arm ran once: arm or luck, unknowable.
    assert pooled.noise.heat is None and 0 < pooled.band("A", "B") < 2.5
    assert not pooled.distinguishable("A", "B")

    two_heats = [{"a1": 1.0, "a2": 1.0, "b1": 0.75, "b2": 0.75}] * 3
    pooled = pool({"claude": two_heats, "codex": two_heats}, {"a1": "A", "a2": "A", "b1": "B", "b2": "B"},
                  ungraded={}, resolution=STEP)
    assert pooled.noise.heat == 0.0 and pooled.band("A", "B") == pytest.approx(2 * (2 * STEP**2 / 8) ** 0.5)
    assert not pooled.distinguishable("A", "B")  # half a step apart: below what a grading resolves

    # A point apart, every grading agreeing: beyond the band, but two identical heats each
    # are one chance in six under arms that do not differ.
    apart = [{"a1": 1.0, "a2": 1.0, "b1": 0.0, "b2": 0.0}] * 3
    pooled = pool({"claude": apart, "codex": apart}, {"a1": "A", "a2": "A", "b1": "B", "b2": "B"},
                  ungraded={}, resolution=STEP)
    assert pooled.band("A", "B") < 1.0 and not pooled.distinguishable("A", "B")


def test_one_pass_one_grader_leaves_only_the_heats_spread() -> None:
    pooled = pool({"g": [{"a1": 1.0, "a2": 3.0}]}, {"a1": "A", "a2": "A"}, ungraded={}, resolution=STEP)
    assert pooled.arms["A"].se == pytest.approx(1.0)


def test_a_grading_that_misses_an_output_or_a_short_grader_is_not_pooled() -> None:
    arm_of = {"a1": "A", "a2": "A"}
    with pytest.raises(ValueError, match="not every output"):
        pool({"g": [{"a1": 1.0}]}, arm_of, ungraded={}, resolution=STEP)
    with pytest.raises(ValueError, match="same number of passes"):
        pool({"g": [{"a1": 1.0, "a2": 0.0}], "h": [{"a1": 1.0, "a2": 0.0}] * 2}, arm_of, ungraded={}, resolution=STEP)
    with pytest.raises(ValueError, match="resolves some step"):
        pool({"g": [{"a1": 1.0, "a2": 0.0}]}, arm_of, ungraded={}, resolution=0)


def test_a_tier_ends_only_where_every_arm_above_is_told_apart_from_every_arm_below() -> None:
    """A (3, unsure) overlaps B and C, but B (2.6) and C (2.2) are told apart:
    "A > C" would be false, so no tier boundary can fall between them."""
    means = {"A": 3.0, "B": 2.6, "C": 2.2, "D": 0.0}
    apart = {("B", "C"), ("A", "D"), ("B", "D"), ("C", "D")}

    def told(p: str, q: str) -> bool:
        return (p, q) in apart or (q, p) in apart

    assert rank(means, distinguishable=told) == (("A", "B", "C"), ("D",))
    # A is told apart from C but B is not: a boundary before C would claim B > C.
    only_a_c = {("A", "C"), ("C", "A")}
    assert rank({"A": 3.0, "B": 2.6, "C": 2.2}, distinguishable=lambda p, q: (p, q) in only_a_c) == (("A", "B", "C"),)


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
    assert finding_ids('{"findings": null, "design_findings": "x"}') == frozenset()


def test_the_ranking_text_never_states_an_order_the_pairs_do_not_hold() -> None:
    from issue_orchestrator.contracts.improver_tournament import TournamentResult

    def comparison(hi: str, lo: str, apart: bool) -> dict[str, object]:
        return {"higher": hi, "lower": lo, "gap": 1.0, "band": 1.0, "heat_p": 0.05, "distinguishable": apart}

    result = TournamentResult.model_validate({
        "tournament_id": "t", "snapshot_id": "s", "key_items": 1, "max_score": 3, "graders": [], "passes": 3,
        "noise": {"heat": 0.1, "grader": 0.0, "pass_": 0.0, "resolution": 0.5}, "band_ses": 2.0, "heat_alpha": 0.05,
        "arms": [],
        "comparisons": [comparison("A", "B", False), comparison("A", "C", False), comparison("B", "C", True),
                        comparison("A", "D", True), comparison("B", "D", True), comparison("C", "D", True)],
        "ranking": [["A", "B", "C"], ["D"]],
        "cost": {"arm_heats": {}, "grader_calls": {}, "grader_seconds": {}},
    })

    # A is not told apart from C, so nothing reads "A > C" or "A ≈ B ≈ C" (B > C holds).
    assert result.ranking_text() == "{A, B, C: B>C} > D"
    assert result.distinguishable == (("B", "C"), ("A", "D"), ("B", "D"), ("C", "D"))


def test_an_arms_shown_error_is_never_below_its_own_heats() -> None:
    both = [{"x1": 0.0, "x2": 4.0, "y1": 1.5, "y2": 1.5}]
    arm_of = {"x1": "X", "x2": "X", "y1": "Y", "y2": "Y"}
    quiet = {f"q{i}": "Q" for i in range(6)}

    alone = pool({"g": both, "h": both}, arm_of, ungraded={}, resolution=STEP)
    crowded = pool({g: [{**r, **{q: 0.0 for q in quiet}} for r in both] for g in ("g", "h")},
                   {**arm_of, **quiet}, ungraded={}, resolution=STEP)

    # X's own heats: variance 8 over 2 -> se 2, whatever the others show.
    assert alone.arms["X"].se >= 2.0 and crowded.arms["X"].se >= 2.0


def test_an_arm_with_few_noisy_heats_is_not_steadied_by_a_steady_arms_many() -> None:
    """A's three heats (1, 1, 7) are noisy: its own standard error is 2,
    whatever twenty steady B heats show."""
    runs = [{**{f"a{i}": v for i, v in enumerate((1.0, 1.0, 7.0))}, **{f"b{i}": 0.0 for i in range(20)}}] * 3
    arm_of = {**{f"a{i}": "A" for i in range(3)}, **{f"b{i}": "B" for i in range(20)}}

    pooled = pool({"claude": runs, "codex": runs}, arm_of, ungraded={}, resolution=STEP)

    assert pooled.arms["A"].mean == 3.0 and pooled.heat_p("A", "B") < 0.001
    assert pooled.band("A", "B") >= 4.0 and not pooled.distinguishable("A", "B")


def test_the_default_heats_can_separate_a_clear_winner() -> None:
    from issue_orchestrator.entrypoints.cli_tools import improver_tournament as cli

    heats = cli.build_parser().parse_args(["run", "--snapshot", "s", "--arm", "A=claude:opus:scripted"]).heats
    runs = [{**{f"a{i}": 10.0 for i in range(heats)}, **{f"b{i}": 0.0 for i in range(heats)}}] * 3
    arm_of = {**{f"a{i}": "A" for i in range(heats)}, **{f"b{i}": "B" for i in range(heats)}}

    assert pool({"claude": runs, "codex": runs}, arm_of, ungraded={}, resolution=STEP).distinguishable("A", "B")


def test_more_heats_never_make_a_graders_precision_finer() -> None:
    """Every grading gives A's three heats 0.5 and B's 0: no noise is seen,
    but one half-step is what a grading resolves, however many heats share it."""
    runs = [{"a1": 0.5, "a2": 0.5, "a3": 0.5, "b1": 0.0, "b2": 0.0, "b3": 0.0}] * 3
    arm_of = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "b2": "B", "b3": "B"}

    pooled = pool({"claude": runs, "codex": runs}, arm_of, ungraded={}, resolution=STEP)

    assert pooled.heat_p("A", "B") == pytest.approx(1 / 20)
    assert pooled.band("A", "B") == pytest.approx(0.5) and not pooled.distinguishable("A", "B")


@pytest.mark.parametrize("source", ["", "   ", "\n\t"])
def test_an_observation_cites_where_its_time_is_read(source: str) -> None:
    """#8972: an item's observable_since is only as good as its citation."""
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from issue_orchestrator.contracts.improver_tournament import Observation

    at = datetime(2026, 10, 4, 9, 2, tzinfo=UTC)
    with pytest.raises(ValidationError, match="at least 1 character"):
        Observation(at=at, source=source)
    assert Observation(at=at, source="  porchpin/porchpin#479 created_at ").source == "porchpin/porchpin#479 created_at"
