"""Live: io's tournament reproduces the 2026-10-04 improver tournament's ranking (#8001).

Not part of ``make validate-pr``: it spends two real grader runs (one
Claude, one Codex). Run it with ``make test-improver-tournament``
(``E2E_IMPROVER_TOURNAMENT=1``; ``E2E_IMPROVER_AB`` names the 2026-10-04
tournament's directory, default ``~/dev/improver-ab``, and
``E2E_IMPROVER_AB_KEY`` its sealed key).

In a store of its own, it freezes the tournament's snapshot, seeds the
sealed key (no hindsight item), and grades the twelve recorded answers
exactly as the tournament's graders read them, through io's anonymizer and
cross-model graders. The ranking must be the tournament's: C > B > A ≈ D.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools import improver_tournament as cli

ENABLED = os.environ.get("E2E_IMPROVER_TOURNAMENT") == "1"
AB = Path(os.environ.get("E2E_IMPROVER_AB", str(Path.home() / "dev" / "improver-ab")))
SEALED_KEY = Path(os.environ.get(
    "E2E_IMPROVER_AB_KEY", str(Path.home() / "dev/worktree/issue-orchestrator/.coord/ab/grading-key-20261004.md")
))


@pytest.mark.live_agent
@pytest.mark.skipif(not ENABLED, reason="Set E2E_IMPROVER_TOURNAMENT=1 (make test-improver-tournament) to run it.")
@pytest.mark.timeout(90 * 60)
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

    [result] = list((store / "tournaments").glob("*/result.json"))
    ranking = json.loads(result.read_text())["ranking"]
    print(f"\n[IMPROVER TOURNAMENT] {datetime.now(UTC).isoformat()} ranking {ranking}", flush=True)
    assert ranking[0] == ["C"] and ranking[1] == ["B"] and set(ranking[2]) == {"A", "D"}
