"""One budgeted improver run over every running engine (#7567).

Two engines, as on the operator's machine: io's own and a porchpin-shaped
target repository's. Each is staged from byte copies of its own state,
audited, and run through the improver separately, tagged with its engine.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.adapters.registered_engine_inventory import engine_at
from issue_orchestrator.contracts.improver_run import RunOutcome
from issue_orchestrator.domain.engine_activity import EngineInventoryRead, EngineRef, EngineSighting
from issue_orchestrator.entrypoints.engine_activity_probe import SnapshotEngineActivityProbe
from issue_orchestrator.entrypoints.improver_run import ImproverRun
from issue_orchestrator.entrypoints.improver_staging import ImproverInputStager
from issue_orchestrator.entrypoints.improver_sweep import (
    EXIT_OK,
    EXIT_UNAVAILABLE,
    ImproverSweep,
    ImproverSweepRequest,
)
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.ports.improver import ImproverAgentResult
from tests.unit.entrypoints.test_improver_staging import (
    COMMIT,
    NOW,
    STARTED,
    FakeHost,
    FakeSource,
    _fingerprint,
    make_engine_state,
)
from tests.unit.improver_support import FakeIssueHost, MemoryRunStore

OUTPUTS = "issue-orchestrator/issue-orchestrator"


class Inventory:
    """Engines with when each last wrote its log (None: running)."""

    def __init__(self, *engines: EngineRef, written: dict[str, datetime] | None = None) -> None:
        self._engines = engines
        self._written = written or {}
        self.asked: list[datetime] = []

    def engines(self, *, since: datetime) -> EngineInventoryRead:
        self.asked.append(since)
        sightings = (
            EngineSighting(
                engine,
                running=engine.engine_id not in self._written,
                last_written=self._written.get(engine.engine_id),
            )
            for engine in self._engines
        )
        return EngineInventoryRead(sightings=tuple(s for s in sightings if s.active_since(since)))


class EmptyFindingsAgent:
    """Writes no finding, after reading which engine it was given."""

    def __init__(self) -> None:
        self.engines: list[tuple[str, str]] = []

    def run(self, *, prompt: str, run_dir: Path) -> ImproverAgentResult:
        inputs = json.loads((run_dir / "improver-data" / "inputs.json").read_text())
        self.engines.append((inputs["engine_id"], inputs["audited_repo"]))
        return ImproverAgentResult(json.dumps({
            "schema_version": 2, "engine_commit": COMMIT, "engine_started_at": STARTED.isoformat(),
            "findings": [],
            "trend": {"exam_scores": "unobserved", "operator_interventions": "unobserved", "notes": ""},
        }), "done")


@pytest.fixture
def two_engines(tmp_path: Path) -> tuple[EngineRef, EngineRef]:
    io = engine_at(make_engine_state(tmp_path / "issue-orchestrator", OUTPUTS), OUTPUTS)
    porchpin = engine_at(make_engine_state(tmp_path / "porchpin"), "porchpin/porchpin")
    return io, porchpin


def _sweep(
    store: MemoryRunStore, agent: EmptyFindingsAgent, inventory: Inventory, now: datetime = NOW
) -> ImproverSweep:
    def run_for(engine: EngineRef) -> ImproverRun:
        return ImproverRun(
            store=store,
            stager=ImproverInputStager(
                audited_host=FakeHost(), outputs_host=FakeHost(), source=FakeSource(), clock=lambda: NOW,
            ),
            agent=agent,
            effects=ImproverEffects(store=store, host=FakeIssueHost(), outputs_repo=OUTPUTS, clock=lambda: NOW),
            prompt="PROMPT",
            clock=lambda: NOW,
        )

    return ImproverSweep(inventory=inventory, runs=store, run_for=run_for, clock=lambda: now)


def _request() -> ImproverSweepRequest:
    return ImproverSweepRequest(
        outputs_repo=OUTPUTS, exam_dir=None, window=timedelta(hours=24),
        log_tail_bytes=1024 * 1024, recent=timedelta(hours=24),
    )


def test_every_running_engine_is_staged_audited_and_run_separately(
    two_engines: tuple[EngineRef, EngineRef], tmp_path: Path
) -> None:
    io, porchpin = two_engines
    before = _fingerprint(porchpin.state_dir)
    store, agent = MemoryRunStore(tmp_path / "store"), EmptyFindingsAgent()

    result = _sweep(store, agent, Inventory(io, porchpin)).sweep(_request())

    assert result.exit_code == EXIT_OK
    assert agent.engines == [(io.engine_id, OUTPUTS), (porchpin.engine_id, "porchpin/porchpin")]
    by_engine = {run.engine_id: run for run in result.runs}
    assert set(by_engine) == {io.engine_id, porchpin.engine_id}
    target = by_engine[porchpin.engine_id]
    assert target.outcome is RunOutcome.ACCEPTED and target.audited_repo == "porchpin/porchpin"
    staged = Path(target.run_dir) / "improver-data"
    audit = json.loads((staged / "audit.json").read_text())
    assert audit["repo"] == "porchpin/porchpin" and audit["state_dir"] == str(porchpin.state_dir)
    assert json.loads((staged / "inputs.json").read_text())["engine_id"] == porchpin.engine_id
    assert Path(by_engine[io.engine_id].run_dir) != Path(target.run_dir)
    # The target engine's state is only read.
    assert _fingerprint(porchpin.state_dir) == before


def test_an_engine_that_acted_after_its_last_accepted_run_is_swept_however_long_ago_it_stopped(
    two_engines: tuple[EngineRef, EngineRef], tmp_path: Path
) -> None:
    """r2 F1: porchpin was accepted, wrote its log a minute later, then stopped;
    a sweep more than a day later still audits it. io, stopped before its own
    last accepted run, is not audited again."""
    from datetime import timedelta as td

    io, porchpin = two_engines
    store = MemoryRunStore(tmp_path / "store")
    first = _sweep(store, EmptyFindingsAgent(), Inventory(io, porchpin)).sweep(_request())
    accepted_at = {r.engine_id: r.started_at for r in first.runs}
    later = Inventory(io, porchpin, written={
        porchpin.engine_id: accepted_at[porchpin.engine_id] + td(minutes=1),
        io.engine_id: accepted_at[io.engine_id] - td(minutes=1),
    })
    agent = EmptyFindingsAgent()

    result = _sweep(store, agent, later, now=NOW + td(hours=24, minutes=2)).sweep(_request())

    assert [e.engine_id for e in result.engines] == [porchpin.engine_id]
    assert agent.engines == [(porchpin.engine_id, "porchpin/porchpin")]
    assert later.asked == [min(accepted_at.values())]


def test_an_engine_that_cannot_be_identified_keeps_the_sweep_from_green(
    two_engines: tuple[EngineRef, EngineRef], tmp_path: Path
) -> None:
    """r3 F1: an engine in scope with no start record of this version is named, not guessed."""
    from issue_orchestrator.domain.engine_activity import UnidentifiedEngine

    io, _ = two_engines
    missing = UnidentifiedEngine(tmp_path / "old" / ".issue-orchestrator" / "state", "no engine-start.json")

    class WithOld(Inventory):
        def engines(self, *, since: datetime) -> EngineInventoryRead:
            read = super().engines(since=since)
            return EngineInventoryRead(sightings=read.sightings, unidentified=(missing,))

    result = _sweep(MemoryRunStore(tmp_path / "store"), EmptyFindingsAgent(), WithOld(io)).sweep(_request())

    assert [r.outcome for r in result.runs] == [RunOutcome.ACCEPTED]
    assert result.unidentified == (missing,) and result.exit_code == EXIT_UNAVAILABLE


def test_no_engine_to_audit_is_unavailable(tmp_path: Path) -> None:
    result = _sweep(MemoryRunStore(tmp_path), EmptyFindingsAgent(), Inventory()).sweep(_request())

    assert result.runs == () and result.exit_code == EXIT_UNAVAILABLE


def test_the_activity_probe_reads_every_engine_from_copies(
    two_engines: tuple[EngineRef, EngineRef],
) -> None:
    io, porchpin = two_engines
    before = _fingerprint(porchpin.state_dir)

    observation = SnapshotEngineActivityProbe(Inventory(io, porchpin)).observe(
        now=NOW, since=NOW - timedelta(hours=24)
    )

    assert [e.engine_id for e in observation.engines] == [io.engine_id, porchpin.engine_id]
    target = observation.engine(porchpin.engine_id)
    assert target is not None and target.repo == "porchpin/porchpin"
    assert target.decisions == 2  # both recorded charter decisions, not just the window's
    assert target.completions is None  # no validated work store: unobserved, never zero
    assert _fingerprint(porchpin.state_dir) == before
