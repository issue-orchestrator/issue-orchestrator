"""One improver run: stage, agent, validate, record, apply (#7490)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_findings import FINDINGS_FILE
from issue_orchestrator.contracts.improver_run import EffectStatus, ImproverAgentChoice, ImproverProvider, RunOutcome
from issue_orchestrator.domain.engine_activity import EngineRef
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.entrypoints.improver_run import HeatPlan, ImproverRun, ImproverRunRequest, findings_text, render_run
from issue_orchestrator.entrypoints.improver_staging import (
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    StagedImproverInputs,
    load_staged_evidence,
)
from issue_orchestrator.contracts.improver_toolbox import ImproverMode
from issue_orchestrator.execution.improver_investigation import ScriptedInvestigation
from issue_orchestrator.ports.improver_investigation import InvestigationKit
from issue_orchestrator.ports.improver_toolbox import ToolboxEndpoint
from issue_orchestrator.ports.improver import HeatSpace, ImproverAgentResult
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
    choice = ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="opus")

    def __init__(self, message: str | None, detail: str = "claude finished") -> None:
        self.message = message
        self.detail = detail
        self.prompts: list[str] = []
        self.toolboxes: list[object] = []

    def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
        self.prompts.append(prompt)
        self.toolboxes.append(toolbox)
        return ImproverAgentResult(self.message, self.detail)


def _request(audited_repo: str = "porchpin/porchpin", engine_id: str | None = None) -> ImproverRunRequest:
    engine = EngineRef(
        engine_id=engine_id or f"repo-{audited_repo.replace('/', '-')}",
        repo=audited_repo,
        state_dir=Path("/engine/state"),
    )
    return ImproverRunRequest(
        engine=engine,
        outputs_repo="issue-orchestrator/issue-orchestrator", exam_dir=None,
        window=timedelta(hours=24), log_tail_bytes=1024,
    )


_RUNS = iter(range(10**6))


def _improver(
    store: MemoryRunStore,
    host: FakeIssueHost,
    agent: FakeAgent,
    stager: FakeStager | None = None,
    investigation: object | None = None,
    heats: HeatPlan | None = None,
) -> ImproverRun:
    # Each improver's clock starts a day after the previous one's, so runs order by time.
    start = NOW + timedelta(days=next(_RUNS))
    clock = iter(start + timedelta(minutes=i) for i in range(1000))
    return ImproverRun(
        store=store,
        stager=stager or FakeStager(),
        agent=agent,
        investigation=investigation or ScriptedInvestigation(),  # type: ignore[arg-type]
        effects=ImproverEffects(
            store=store, host=host, outputs_repo="issue-orchestrator/issue-orchestrator", clock=lambda: NOW
        ),
        prompt="THE PROMPT",
        heats=heats or HeatPlan(count=1, parallel=1),
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


def test_a_run_never_starts_while_another_holds_the_store(tmp_path: Path) -> None:
    from issue_orchestrator.execution.improver_run_store import FileImproverRunStore
    from issue_orchestrator.ports.improver import ImproverStoreBusy
    import pytest

    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(_findings("capability_issue"))

    with FileImproverRunStore(tmp_path).exclusive():
        with pytest.raises(ImproverStoreBusy):
            _improver(store, host, agent).run(_request())

    assert agent.prompts == [] and store.runs() == ()


def test_a_run_whose_effects_an_earlier_failure_blocks_is_unavailable(tmp_path: Path) -> None:
    """An older owed effect keeps failing, so this run's are not tried: it
    must not exit green (r1 F4)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    host.fail_on_create = RuntimeError("down")
    _improver(store, host, FakeAgent(_findings("capability_issue"))).run(_request())

    current = _improver(store, host, FakeAgent(_findings("prompt_proposal"))).run(_request())

    assert current.outcome is RunOutcome.ACCEPTED and current.exit_code == 75
    assert host.comments == []
    assert "earlier run" in current.effects[0].detail
    host.fail_on_create = None


def test_the_previous_audit_and_grades_are_the_same_engines(tmp_path: Path) -> None:
    """A, then B (accepted, other grades), then A: A diffs and grades against A (r1 F5, r2 F3)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    first = _improver(store, host, FakeAgent(_findings("exam_case"))).run(_request("a/a"))
    _improver(store, host, FakeAgent(_findings("prompt_proposal"))).run(_request("b/b"))
    _improver(store, host, FakeAgent("not json")).run(_request("b/b"))
    stager = FakeStager()

    third = _improver(store, host, FakeAgent(_findings("charter_proposal")), stager).run(_request("a/a"))

    assert stager.requests[0].previous_audit == Path(first.run_dir) / "improver-data" / "audit.json"
    assert {m.stall_point: m.previous for m in third.stall_points} == {
        "noticed_not_acted": 1, "not_noticed": 0,
    }


def test_two_engines_of_one_repository_never_diff_against_each_other(tmp_path: Path) -> None:
    """#7567: engines are keyed by their Control Center key, not their repository
    (an io worktree's engine and io's own both work issue-orchestrator)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    first = _improver(store, host, FakeAgent(_findings("exam_case"))).run(_request("a/a", "repo-one"))
    _improver(store, host, FakeAgent(_findings("prompt_proposal"))).run(_request("a/a", "repo-two"))
    stager = FakeStager()

    third = _improver(store, host, FakeAgent(_findings("charter_proposal")), stager).run(
        _request("a/a", "repo-one")
    )

    assert third.engine_id == "repo-one"
    assert stager.requests[0].previous_audit == Path(first.run_dir) / "improver-data" / "audit.json"
    assert {m.stall_point: m.previous for m in third.stall_points} == {
        "noticed_not_acted": 1, "not_noticed": 0,
    }


def test_a_run_with_nothing_of_its_own_is_not_green_while_an_earlier_run_is_owed(tmp_path: Path) -> None:
    """r2 F1: an accepted empty findings file, an older effect still failing."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    host.fail_on_create = RuntimeError("down")
    older = _improver(store, host, FakeAgent(_findings("capability_issue"))).run(_request())
    empty = json.loads(_findings("capability_issue"))
    empty["findings"] = []

    current = _improver(store, host, FakeAgent(json.dumps(empty))).run(_request())

    assert current.effects == () and current.exit_code == 75
    assert current.owed_by_earlier_runs == (older.run_id,)
    assert f"still owed by earlier runs: {older.run_id}" in render_run(current)


def test_the_rendered_run_names_an_effects_error(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    host.fail_on_create = RuntimeError("500")

    record = _improver(store, host, FakeAgent(_findings("capability_issue"))).run(_request())

    assert "error: RuntimeError: 500" in render_run(record)


def test_a_run_for_another_repository_than_its_effects_is_refused(tmp_path: Path) -> None:
    import pytest

    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    request = ImproverRunRequest(
        engine=EngineRef("repo-a", "a/a", Path("/s")), outputs_repo="other/repo", exam_dir=None,
        window=timedelta(hours=24), log_tail_bytes=1,
    )

    with pytest.raises(ValueError, match="other/repo"):
        _improver(store, host, FakeAgent("{}")).run(request)


def test_an_agent_that_cannot_be_launched_is_recorded_unavailable(tmp_path: Path) -> None:
    """3b r6 F1: e.g. the permission profile refuses a Codex config."""

    class Refused:
        choice = FakeAgent.choice

        def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
            raise RuntimeError("legacy sandbox_mode disables the permission profile")

    store, host = MemoryRunStore(tmp_path), FakeIssueHost()

    record = _improver(store, host, Refused()).run(_request())  # type: ignore[arg-type]

    [stored] = store.runs()
    assert stored.outcome is RunOutcome.AGENT_FAILED and stored.exit_code == 75
    assert "sandbox_mode" in stored.detail and stored.effects == ()
    assert record == stored


def _blind(request: ImproverRunRequest, *hidden: int) -> ImproverRunRequest:
    from dataclasses import replace

    return replace(request, excluded_open_issues=frozenset(hidden))


def test_a_blind_run_is_graded_and_recorded_but_owes_github_nothing(tmp_path: Path) -> None:
    """Handover tests hide the issues that track the defects; a finding the
    improver then makes would duplicate them, so nothing may ever be filed."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    stager = FakeStager()

    record = _improver(store, host, FakeAgent(_findings("exam_case")), stager).run(
        _blind(_request(), 7592, 7593), apply=False
    )

    assert stager.requests[0].excluded_open_issues == frozenset({7592, 7593})
    assert record.outcome is RunOutcome.ACCEPTED and [g.stall_point for g in record.grades] == ["noticed_not_acted"]
    assert record.effects == () and record.blind_excluded_issues == (7592, 7593)
    assert "blind: hid #7592, #7593" in render_run(record)
    # A later real run applies what is owed: the blind run owes nothing.
    _improver(store, host, FakeAgent(_findings("prompt_proposal"))).run(_request())
    assert all("Exam case" not in c["title"] for c in host.created)


def test_a_blind_run_refuses_to_apply(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="blind"):
        _improver(MemoryRunStore(tmp_path), FakeIssueHost(), FakeAgent("{}")).run(_blind(_request(), 7592))


def test_a_blind_runs_grades_are_not_the_next_runs_baseline(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _improver(store, host, FakeAgent(_findings("exam_case"))).run(_blind(_request(), 7592), apply=False)

    record = _improver(store, host, FakeAgent(_findings("exam_case"))).run(_request())

    assert [(m.stall_point, m.previous) for m in record.stall_points] == [("noticed_not_acted", None)]


@pytest.mark.parametrize(
    "choice",
    [
        ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="opus"),
        ImproverAgentChoice(provider=ImproverProvider.CODEX, model="gpt-5.6-sol"),
    ],
    ids=lambda c: c.describe(),
)
def test_every_run_records_the_provider_and_model_it_ran_on(tmp_path: Path, choice: ImproverAgentChoice) -> None:
    """Grades and trends compare like with like only if each run says which
    agent produced it (#8001), whatever its outcome."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    accepted = FakeAgent(_findings("exam_case"))
    accepted.choice = choice
    failed = FakeAgent(None, "timed out")
    failed.choice = choice

    records = [_improver(store, host, agent).run(_request()) for agent in (accepted, failed)]

    assert [r.outcome for r in records] == [RunOutcome.ACCEPTED, RunOutcome.AGENT_FAILED]
    assert [r.agent for r in store.runs()] == [choice, choice]
    assert f"agent {choice.describe()}" in render_run(records[0])


def test_a_change_of_agent_starts_a_new_stall_point_baseline(tmp_path: Path) -> None:
    """r1 F3: a Codex run's grades are no baseline for a Claude run's; a
    change of provider or model is not a change in the engine."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    codex = FakeAgent(_findings("exam_case"))
    codex.choice = ImproverAgentChoice(provider=ImproverProvider.CODEX, model="gpt-5.6-sol")
    _improver(store, host, codex).run(_request(), apply=False)
    claude = FakeAgent(_findings("exam_case"))

    first_claude = _improver(store, host, claude).run(_request(), apply=False)
    second_claude = _improver(store, host, claude).run(_request(), apply=False)

    assert {m.previous for m in first_claude.stall_points} == {None}
    assert [(m.stall_point, m.previous) for m in second_claude.stall_points] == [("noticed_not_acted", 1)]


class FakeEmpowered:
    """An empowered investigation whose toolbox is a fixed endpoint."""

    mode = ImproverMode.EMPOWERED

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.opened: list[tuple[str, Path]] = []
        self.hidden: list[frozenset[int]] = []
        self.closed = 0

    @contextmanager
    def open(self, engine: EngineRef, run_dir: Path, *, hidden_issues: frozenset[int]) -> Iterator[InvestigationKit]:
        if self.fail:
            raise self.fail
        self.opened.append((engine.repo, run_dir))
        self.hidden.append(hidden_issues)
        try:
            yield InvestigationKit(
                toolbox=ToolboxEndpoint(url="http://127.0.0.1:1/mcp", token="t"), instructions="\n\nEMPOWERED ADDENDUM"
            )
        finally:
            self.closed += 1


def test_an_empowered_run_gives_the_agent_its_toolbox_and_instructions_and_records_the_mode(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(_findings("exam_case"))
    empowered = FakeEmpowered()

    record = _improver(store, host, agent, investigation=empowered).run(_request(), apply=False)

    assert record.outcome is RunOutcome.ACCEPTED and record.mode is ImproverMode.EMPOWERED
    [(repo, run_dir)] = empowered.opened
    assert repo == "porchpin/porchpin" and str(run_dir) == record.run_dir
    assert agent.prompts[0].endswith("THE PROMPT\n\nEMPOWERED ADDENDUM")
    assert agent.toolboxes == [ToolboxEndpoint(url="http://127.0.0.1:1/mcp", token="t")]
    # The toolbox is served only while the agent runs.
    assert empowered.closed == 1
    assert "claude:opus, empowered" in render_run(record)


def test_a_toolbox_that_cannot_be_prepared_ends_the_run_unavailable_with_no_agent(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(_findings("exam_case"))

    record = _improver(
        store, host, agent, investigation=FakeEmpowered(fail=RuntimeError("clone failed"))
    ).run(_request(), apply=False)

    assert record.outcome is RunOutcome.UNAVAILABLE and record.exit_code == 75
    assert "toolbox unavailable: RuntimeError: clone failed" in record.detail
    assert agent.prompts == []


def test_a_change_of_mode_starts_a_new_stall_point_baseline(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(_findings("exam_case"))
    _improver(store, host, agent).run(_request(), apply=False)

    empowered = _improver(store, host, agent, investigation=FakeEmpowered()).run(_request(), apply=False)

    assert {m.previous for m in empowered.stall_points} == {None}


def test_a_blind_run_hides_its_issues_from_the_toolbox_when_it_audits_its_outputs_repo(tmp_path: Path) -> None:
    """r5 F1: a blind run of io auditing itself must not let the toolbox's
    GitHub reads show the hidden issues; auditing another repository, the
    numbers name other issues, so nothing is hidden there."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    empowered = FakeEmpowered()
    io = "issue-orchestrator/issue-orchestrator"
    for audited in (io, "porchpin/porchpin"):
        request = _request(audited_repo=audited)
        blind = ImproverRunRequest(
            engine=request.engine, outputs_repo=io, exam_dir=None, window=request.window,
            log_tail_bytes=1024, excluded_open_issues=frozenset({7592}),
        )
        _improver(store, host, FakeAgent(_findings("exam_case")), investigation=empowered).run(blind, apply=False)

    assert empowered.hidden == [frozenset({7592}), frozenset()]



class HeatAgent:
    """Answers per heat, and records how many heats ran at once."""

    choice = ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="opus")

    def __init__(self, answers: dict[int, str | None], hold: float = 0.2) -> None:
        import threading

        self.answers = answers
        self.hold = hold
        self.heats: list[int] = []
        self.spaces: list[HeatSpace] = []
        self.running = 0
        self.most_at_once = 0
        self._lock = threading.Lock()

    def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
        import time

        with self._lock:
            self.heats.append(space.heat)
            self.spaces.append(space)
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        time.sleep(self.hold)
        with self._lock:
            self.running -= 1
        answer = self.answers[space.heat]
        if answer == "RAISE":
            raise RuntimeError("binary missing")
        return ImproverAgentResult(answer, "done" if answer else "timed out")


def test_heats_run_in_parallel_up_to_the_plan_and_their_findings_merge_with_support(tmp_path: Path) -> None:
    """#8001: N heats on the same inputs; a finding several found is kept once
    and says so; each heat's answer is kept beside the merged one."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = HeatAgent({1: _findings("exam_case"), 2: _findings("exam_case", "capability_issue"), 3: _findings("exam_case")})

    record = _improver(store, host, agent, heats=HeatPlan(count=3, parallel=2)).run(_request(), apply=False)

    assert sorted(agent.heats) == [1, 2, 3] and agent.most_at_once == 2
    assert record.outcome is RunOutcome.ACCEPTED
    assert "from 3 of 3 heat(s)" in record.detail
    assert [(h.heat, h.outcome, h.findings) for h in record.heats] == [
        (1, RunOutcome.ACCEPTED, 1), (2, RunOutcome.ACCEPTED, 2), (3, RunOutcome.ACCEPTED, 1),
    ]
    exam = example("exam_case")["findings"][0]["id"]
    capability = example("capability_issue")["findings"][0]["id"]
    assert {s.finding_id: s.heats for s in record.finding_support} == {exam: (1, 2, 3), capability: (2,)}
    run_dir = Path(record.run_dir)
    assert all((run_dir / f"improver-findings-h{h}.json").is_file() for h in (1, 2, 3))
    merged = json.loads((run_dir / "improver-findings.json").read_text())
    assert [f["id"] for f in merged["findings"]] == [exam, capability]
    assert f"{exam}: found by 3 of 3 heat(s)" in render_run(record)
    assert len(record.effects) == 2


def test_a_run_is_accepted_if_any_heat_is_and_keeps_why_the_others_were_not(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = HeatAgent({1: "not json", 2: None, 3: "RAISE", 4: _findings("capability_issue")}, hold=0)

    record = _improver(store, host, agent, heats=HeatPlan(count=4, parallel=4)).run(_request(), apply=False)

    assert record.outcome is RunOutcome.ACCEPTED and "from 1 of 4 heat(s)" in record.detail
    outcomes = {h.heat: (h.outcome, h.detail) for h in record.heats}
    assert outcomes[1][0] is RunOutcome.REJECTED and outcomes[2] == (RunOutcome.AGENT_FAILED, "timed out")
    assert outcomes[3] == (RunOutcome.AGENT_FAILED, "agent not launched: RuntimeError: binary missing")
    assert record.heats[0].rejections


def test_no_accepted_heat_is_a_rejection_when_any_answered_and_a_failure_otherwise(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()

    rejected = _improver(store, host, HeatAgent({1: "not json", 2: None}, hold=0), heats=HeatPlan(2, 2)).run(
        _request(), apply=False
    )
    failed = _improver(store, host, HeatAgent({1: None, 2: "RAISE"}, hold=0), heats=HeatPlan(2, 2)).run(
        _request(), apply=False
    )

    assert rejected.outcome is RunOutcome.REJECTED and rejected.effects == ()
    assert all(r.startswith("heat 1: ") for r in rejected.rejections)
    assert failed.outcome is RunOutcome.AGENT_FAILED
    assert failed.detail == "heat 1: timed out; heat 2: agent not launched: RuntimeError: binary missing"


def test_a_filed_issue_says_how_many_heats_found_its_finding(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = HeatAgent({1: _findings("exam_case"), 2: _findings("exam_case", "capability_issue")}, hold=0)

    _improver(store, host, agent, heats=HeatPlan(2, 2)).run(_request())

    bodies = {c["title"].split("] ", 1)[1].split(":")[0]: c["body"] for c in host.created}
    assert "**Found by 2 of 2 independent heat(s)** (1, 2)." in next(b for t, b in bodies.items() if t.startswith("Exam case"))
    assert "**Found by 1 of 2 independent heat(s)** (2)." in bodies["Capability gap"]


def test_a_conflict_between_heats_is_recorded_and_shown_on_the_kept_findings_issue(tmp_path: Path) -> None:
    """r1 F2: a different finding proposing the same exam case is not lost."""
    # A valid finding about another anomaly that proposes the same new case.
    other = json.loads(_findings("capability_issue"))
    finding = other["findings"][0]
    finding["id"] = "same-case-other-anomaly"
    finding["output"] = "exam_case"
    finding["reproduction"] = example("exam_case")["findings"][0]["reproduction"]
    finding["proposal"] = "Plant the other anomaly instead."
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = HeatAgent({1: _findings("exam_case"), 2: json.dumps(other)}, hold=0)

    record = _improver(store, host, agent, heats=HeatPlan(2, 2)).run(_request())

    assert record.outcome is RunOutcome.ACCEPTED, record.rejections or [h.rejections for h in record.heats]
    [conflict] = record.heat_conflicts
    assert conflict.heat == 2 and conflict.claim == "Plant the other anomaly instead."
    assert "heat 2 not merged" in render_run(record)
    [issue] = host.created
    assert "**Not merged, to resolve:**" in issue["body"] and "Plant the other anomaly instead." in issue["body"]


def test_a_heat_plan_is_bounded() -> None:
    from issue_orchestrator.entrypoints.improver_run import MAX_HEATS

    assert HeatPlan(5, 2).waves == 3 and HeatPlan(2, 2).waves == 1
    for count, parallel in ((0, 1), (MAX_HEATS + 1, 1), (2, 3), (2, 0)):
        with pytest.raises(ValueError):
            HeatPlan(count, parallel)


def test_a_comment_on_an_existing_issue_carries_the_same_support_and_conflicts(tmp_path: Path) -> None:
    """r2 F1: one rule for new issues and comments on existing ones."""
    from issue_orchestrator.contracts.improver_findings import ImproverFindings
    from issue_orchestrator.control.improver_effects import finding_key, title_token
    from issue_orchestrator.ports.engine_audit import OpenIssueLabels

    other = json.loads(_findings("capability_issue"))
    finding = other["findings"][0]
    finding["id"] = "same-case-other-anomaly"
    finding["output"] = "exam_case"
    finding["reproduction"] = example("exam_case")["findings"][0]["reproduction"]
    finding["proposal"] = "Plant the other anomaly instead."
    kept = ImproverFindings.model_validate_json(_findings("exam_case")).findings[0]
    token = title_token(finding_key(kept, _request().engine))
    store = MemoryRunStore(tmp_path)
    host = FakeIssueHost([OpenIssueLabels(number=901, title=f"{token} Exam case: earlier", labels=())])
    agent = HeatAgent({1: _findings("exam_case"), 2: json.dumps(other)}, hold=0)

    _improver(store, host, agent, heats=HeatPlan(2, 2)).run(_request())

    assert host.created == []
    [(number, body)] = host.comments
    assert number == 901
    assert "**Found by 1 of 2 independent heat(s)** (1)." in body
    assert "**Not merged, to resolve:**" in body and "Plant the other anomaly instead." in body


def test_each_heat_has_its_own_workdir_and_reads_only_the_shared_evidence(tmp_path: Path) -> None:
    """r3 F1: heats are independent support only if none can read another's
    answer. Each runs in heats/h<k>/; what it may read is the staged inputs
    and what the investigation staged, never the run dir or another heat."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = HeatAgent({1: _findings("exam_case"), 2: _findings("exam_case")}, hold=0)

    record = _improver(store, host, agent, heats=HeatPlan(2, 2), investigation=FakeEmpowered()).run(
        _request(), apply=False
    )

    run_dir = Path(record.run_dir).resolve()
    spaces = sorted(agent.spaces, key=lambda s: s.heat)
    assert [s.workdir for s in spaces] == [run_dir / "heats" / "h1", run_dir / "heats" / "h2"]
    for space in spaces:
        assert space.run_dir == run_dir
        assert space.evidence[0] == run_dir / "improver-data"
        others = [o.workdir for o in spaces if o is not space]
        assert not any(o.is_relative_to(root) for o in others for root in space.evidence)
        assert run_dir not in space.evidence
