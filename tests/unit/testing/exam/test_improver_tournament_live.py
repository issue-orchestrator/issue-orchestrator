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
    assert result.ranking[0] == ("C",)
    assert all(("C", arm) in result.distinguishable for arm in "ABD")
    for higher, lower in TEN_FOUR_ABOVE:
        assert (lower, higher) not in result.distinguishable, f"{lower} is distinguishably above {higher}"
