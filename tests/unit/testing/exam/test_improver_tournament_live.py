"""Live: io's tournament reproduces the 2026-10-04 improver tournament's ranking (#8001).

Not part of ``make validate-pr``: it spends six real grader calls (three
Claude, three Codex). Run it with ``make test-improver-tournament``
(``E2E_IMPROVER_TOURNAMENT=1``; ``E2E_IMPROVER_AB`` names the 2026-10-04
tournament's directory, default ``~/dev/improver-ab``, and
``E2E_IMPROVER_AB_KEY`` its sealed key).

In a store of its own, it freezes the tournament's snapshot, seeds the
sealed key (no hindsight item), and grades the twelve recorded answers
through io's anonymizer and cross-model graders, each grader three times
(pooled). The 10-04 ranking was C > B > A ≈ D. Restated within the noise
the harness measures (an arm is ranked above another only when their
pooled means differ by more than twice the standard error of the
difference):

* C ranks first and is distinguishable from every other arm;
* no arm is distinguishably above one 10-04 placed higher (C > B, C > A,
  C > D, B > A, B > D);
* the order among arms indistinguishable near zero is not asserted: their
  differences are within measured grader noise (the 10-04 gradings
  themselves put B, A and D within each other's band).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_tournament import TournamentResult
from issue_orchestrator.entrypoints.cli_tools import improver_tournament as cli

#: 10-04's order: (higher, lower) pairs it placed apart (A ≈ D: no order).
TEN_FOUR_ABOVE = (("C", "B"), ("C", "A"), ("C", "D"), ("B", "A"), ("B", "D"))
ENABLED = os.environ.get("E2E_IMPROVER_TOURNAMENT") == "1"
AB = Path(os.environ.get("E2E_IMPROVER_AB", str(Path.home() / "dev" / "improver-ab")))
SEALED_KEY = Path(os.environ.get(
    "E2E_IMPROVER_AB_KEY", str(Path.home() / "dev/worktree/issue-orchestrator/.coord/ab/grading-key-20261004.md")
))


@pytest.mark.live_agent
@pytest.mark.skipif(not ENABLED, reason="Set E2E_IMPROVER_TOURNAMENT=1 (make test-improver-tournament) to run it.")
@pytest.mark.timeout(150 * 60)
def test_the_2026_10_04_tournament_ranking_is_reproduced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    mapping = json.loads((AB / "results" / "MAPPING.json").read_text())
    for label, output_id in mapping.items():
        shutil.copy(AB / "results" / "anon" / f"{label}.json", recorded / f"{output_id}.txt")
    store = tmp_path / "io-improver"
    monkeypatch.setattr(cli, "improver_root", lambda checkout, runner: store)
    repo = Path(__file__).resolve().parents[4]
    monkeypatch.chdir(repo)

    assert cli.main([
        "snapshot", "import", "--id", "20261004", "--improver-data", str(AB / "snapshot" / "improver-data"),
        "--taken-at", "2026-10-04T07:36:24+00:00", "--origin", "2026-10-04 tournament",
    ]) == 0
    assert cli.main([
        "key", "seed", "--snapshot", "20261004", "--sealed-key", str(SEALED_KEY),
        "--sealed-at", "2026-10-04T07:40:00+00:00", "--by", "coordinator",
    ]) == 0
    assert cli.main(["grade-recorded", "--snapshot", "20261004", "--recorded", str(recorded), "--seed", "20261004"]) == 0

    [path] = list((store / "tournaments").glob("*/result.json"))
    result = TournamentResult.model_validate_json(path.read_text())
    means = {a.arm: a.mean for a in result.arms}
    print(f"\n[IMPROVER TOURNAMENT] {datetime.now(UTC).isoformat()} ranking {result.ranking_text()} means {means}"
          f" se { {a.arm: a.se for a in result.arms} } noise {result.noise}"
          f" distinguishable {result.distinguishable} cost {result.cost}", flush=True)
    assert result.passes == 3 and len(result.graders) == 6
    assert acceptance_failures(result) == []


def acceptance_failures(result: TournamentResult) -> list[str]:
    """The restated 10-04 criterion, as failures: C alone first and told
    apart from every arm; no arm above one 10-04 placed higher by more
    than their band (whatever the heats' test says)."""
    failures = []
    if result.ranking[0] != ("C",):
        failures.append(f"C is not alone first: {result.ranking_text()}")
    failures += [f"C is not told apart from {arm}" for arm in "ABD" if ("C", arm) not in result.distinguishable]
    by_pair = {(c.higher, c.lower): c for c in result.comparisons}
    for higher, lower in TEN_FOUR_ABOVE:
        reversed_ = by_pair.get((lower, higher))
        if reversed_ is not None and reversed_.gap > reversed_.band:
            failures.append(f"{lower} is above {higher} by {reversed_.gap} > band {reversed_.band}")
    return failures


def test_the_acceptance_refuses_a_reversal_beyond_its_band_whatever_the_heats_say() -> None:
    def result(b_over_a: dict[str, object]) -> TournamentResult:
        def c(hi: str, lo: str, **over: object) -> dict[str, object]:
            return {"higher": hi, "lower": lo, "gap": 3.0, "band": 1.0, "heat_p": 0.05, "distinguishable": True,
                    **over}

        return TournamentResult.model_validate({
            "tournament_id": "t", "snapshot_id": "s", "key_items": 1, "max_score": 3, "graders": [], "passes": 3,
            "noise": {"heat": 0.1, "grader": 0.0, "pass_": 0.0, "resolution": 0.5}, "band_ses": 2.0,
            "heat_alpha": 0.05, "arms": [],
            "comparisons": [c("C", "A"), c("C", "B"), c("C", "D"), c("A", "B", **b_over_a),
                            c("B", "D", gap=0.2, distinguishable=False), c("A", "D", gap=0.1, distinguishable=False)],
            "ranking": [["C"], ["A", "B", "D"]],
            "cost": {"arm_heats": {}, "grader_calls": {}, "grader_seconds": {}},
        })

    # A above B (10-04 had B above A) by more than the band, though the heats' test says no.
    beyond = result({"gap": 1.5, "band": 1.0, "heat_p": 0.3, "distinguishable": False})
    within = result({"gap": 0.5, "band": 1.0, "heat_p": 0.3, "distinguishable": False})

    assert acceptance_failures(beyond) == ["A is above B by 1.5 > band 1.0"]
    assert acceptance_failures(within) == []
