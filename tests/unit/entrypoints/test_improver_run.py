"""One improver run: stage, agent, validate, record, apply (#7490)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from issue_orchestrator.contracts.improver_findings import FINDINGS_FILE
from issue_orchestrator.contracts.improver_run import EffectStatus, RunOutcome
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.entrypoints.improver_run import ImproverRun, ImproverRunRequest, findings_text, render_run
from issue_orchestrator.entrypoints.improver_staging import (
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    StagedImproverInputs,
    load_staged_evidence,
)
from issue_orchestrator.ports.improver import ImproverAgentResult
from tests.unit.improver_support import (
    FakeIssueHost,
    MemoryRunStore,
    audits,
    build_improver_data,
    example,
)

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


class FakeStager:
    def __init__(self, unavailable: str | None = None) -> None:
        self.unavailable = unavailable
        self.requests: list[ImproverStagingRequest] = []

    def stage(self, request: ImproverStagingRequest) -> StagedImproverInputs:
        self.requests.append(request)
        if self.unavailable:
            raise ImproverInputsUnavailable(self.unavailable)
        data = build_improver_data(request.run_dir)
        evidence = load_staged_evidence(data)
        _, current = audits()
        assert evidence.audit.generated_at == current.generated_at
        from issue_orchestrator.contracts.improver_inputs import InputsManifest

        manifest = InputsManifest.model_validate_json((data / "inputs.json").read_text())
        return StagedImproverInputs(data_dir=data, manifest=manifest, audit=current)


class FakeAgent:
    def __init__(self, message: str | None, detail: str = "codex finished") -> None:
        self.message = message
        self.detail = detail
        self.prompts: list[str] = []

    def run(self, *, prompt: str, run_dir: Path) -> ImproverAgentResult:
        self.prompts.append(prompt)
        return ImproverAgentResult(self.message, self.detail)


def _request() -> ImproverRunRequest:
    return ImproverRunRequest(
        state_dir=Path("/engine/state"), audited_repo="porchpin/porchpin",
        outputs_repo="issue-orchestrator/issue-orchestrator", exam_dir=None,
        window=timedelta(hours=24), log_tail_bytes=1024,
    )


def _improver(store: MemoryRunStore, host: FakeIssueHost, agent: FakeAgent, stager: FakeStager | None = None) -> ImproverRun:
    clock = iter(NOW + timedelta(minutes=i) for i in range(1000))
    return ImproverRun(
        store=store,
        stager=stager or FakeStager(),
        agent=agent,
        effects=ImproverEffects(store=store, host=host, clock=lambda: NOW),
        prompt="THE PROMPT",
        clock=lambda: next(clock),
    )


def _findings(*names: str) -> str:
    docs = [example(n) for n in names]
    merged = docs[0]
    merged["findings"] = [f for d in docs for f in d["findings"]]
    return json.dumps(merged)


def test_an_accepted_run_is_recorded_graded_and_its_effects_applied(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(_findings("exam_case", "prompt_proposal"))

    record = _improver(store, host, agent).run(_request())

    assert record.outcome is RunOutcome.ACCEPTED and record.outcome.exit_code == 0
    assert [g.stall_point for g in record.grades] == ["noticed_not_acted", "not_in_charter"]
    assert [(m.stall_point, m.previous, m.current) for m in record.stall_points] == [
        ("not_in_charter", None, 1), ("noticed_not_acted", None, 1),
    ]
    assert [e.status for e in record.effects] == [EffectStatus.FILED, EffectStatus.COMMENTED]
    assert len(host.created) == 1 and len(host.comments) == 1
    assert record.engine_commit == "0123456789abcdef0123456789abcdef01234567"
    assert (Path(record.run_dir) / FINDINGS_FILE).is_file()
    assert agent.prompts[0].startswith(f"ISSUE_ORCHESTRATOR_RUN_DIR={record.run_dir}\n\nTHE PROMPT")
    assert "stalled at noticed_not_acted" in render_run(record)


def test_the_next_run_diffs_against_the_last_audit_and_grades_the_trend(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    first = _improver(store, host, FakeAgent(_findings("exam_case"))).run(_request())
    stager = FakeStager()

    second = _improver(store, host, FakeAgent(_findings("charter_proposal")), stager).run(_request())

    assert stager.requests[0].previous_audit == Path(first.run_dir) / "improver-data" / "audit.json"
    moves = {m.stall_point: (m.previous, m.current) for m in second.stall_points}
    assert moves == {"noticed_not_acted": (1, 0), "not_noticed": (0, 1)}


def test_a_rejected_output_is_recorded_with_its_reasons_and_nothing_is_applied(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    doc = json.loads(_findings("exam_case", "capability_issue"))
    doc["findings"][1]["reproduction"]["fails_on"] = "HEAD"

    record = _improver(store, host, FakeAgent(json.dumps(doc))).run(_request())

    assert record.outcome is RunOutcome.REJECTED and record.outcome.exit_code == 1
    assert any("reproduction_fails_on_engine_commit" in r for r in record.rejections)
    assert record.effects == () and record.grades == ()
    assert host.created == [] and host.comments == []


def test_an_agent_that_does_not_finish_is_unavailable(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()

    record = _improver(store, host, FakeAgent(None, "codex timed out after 5400s")).run(_request())

    assert record.outcome is RunOutcome.AGENT_FAILED and record.outcome.exit_code == 75
    assert "timed out" in record.detail
    assert host.created == []


def test_inputs_that_cannot_be_staged_end_the_run_unavailable(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent("{}")

    record = _improver(store, host, agent, FakeStager("no engine-start.json")).run(_request())

    assert record.outcome is RunOutcome.UNAVAILABLE and record.outcome.exit_code == 75
    assert agent.prompts == []
    assert record.audit_staged is False


def test_a_run_first_applies_what_an_earlier_run_still_owes(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    host.fail_on_create = RuntimeError("down")
    owed = _improver(store, host, FakeAgent(_findings("capability_issue"))).run(_request())
    assert owed.outcome is RunOutcome.ACCEPTED and owed.exit_code == 75
    assert owed.pending_effects
    host.fail_on_create = None

    _improver(store, host, FakeAgent(None, "codex exited 1")).run(_request())

    assert [e.status for e in next(r for r in store.runs() if r.run_id == owed.run_id).effects] == [EffectStatus.FILED]


def test_the_findings_are_the_final_message_or_its_one_fenced_block() -> None:
    assert findings_text('  {"a": 1}\n') == '{"a": 1}\n'
    assert findings_text('```json\n{"a": 1}\n```') == '{"a": 1}\n'
    assert findings_text('Here you go:\n```json\n{"a": 1}\n```') == 'Here you go:\n```json\n{"a": 1}\n```\n'


def test_a_run_that_must_not_apply_records_its_effects_as_owed(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()

    record = _improver(store, host, FakeAgent(_findings("capability_issue"))).run(_request(), apply=False)

    assert record.outcome is RunOutcome.ACCEPTED
    assert [e.status for e in record.effects] == [EffectStatus.PENDING]
    assert host.created == [] and host.comments == []
