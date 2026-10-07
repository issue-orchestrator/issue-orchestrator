"""The orchestrator's GitHub effects for the improver's accepted findings (#7490)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from issue_orchestrator.contracts.improver_findings import FINDINGS_FILE, ImproverFindings
from issue_orchestrator.contracts.improver_run import EffectStatus, ImproverRunRecord, RunOutcome
from issue_orchestrator.control.improver_effects import (
    IMPROVER_LABEL,
    OPERATOR_DECISION_LABEL,
    TARGET_OPERATOR_LABEL,
    finding_key,
    planned_effects,
    title_token,
)
from issue_orchestrator.domain.engine_activity import EngineRef
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.domain.host_rate_limit import HostRateLimit
from issue_orchestrator.ports.engine_audit import OpenIssueLabels
from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError
from tests.unit.improver_support import OPEN_TRACKER, FakeIssueHost, MemoryRunStore, example

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


def _engine(repo: str, engine_id: str | None = None) -> EngineRef:
    return EngineRef(engine_id or f"repo-{repo.replace('/', '-')}", repo, Path("/state"))


def _run(
    store: MemoryRunStore, run_id: str, *docs: dict, outcome: RunOutcome = RunOutcome.ACCEPTED,
    audited_repo: str = "porchpin/porchpin", outputs_repo: str = "io/io",
) -> ImproverRunRecord:
    engine = _engine(audited_repo)
    merged = json.loads(json.dumps(docs[0]))
    merged["findings"] = [f for d in docs for f in d["findings"]]
    merged["design_findings"] = [f for d in docs for f in d.get("design_findings", [])]
    findings = ImproverFindings.model_validate_json(json.dumps(merged))
    run_dir = store.new_run_dir(run_id)
    (run_dir / FINDINGS_FILE).write_text(json.dumps(merged))
    record = ImproverRunRecord(
        run_id=run_id, started_at=NOW, finished_at=NOW, outcome=outcome, detail="",
        engine_id=engine.engine_id, audited_repo=audited_repo, outputs_repo=outputs_repo,
        run_dir=str(run_dir), engine_commit=merged["engine_commit"],
        effects=planned_effects(findings, engine) if outcome is RunOutcome.ACCEPTED else (),
    )
    store.record(record)
    return record


def _effects(store: MemoryRunStore, host: FakeIssueHost) -> ImproverEffects:
    return ImproverEffects(store=store, host=host, outputs_repo="io/io", clock=lambda: NOW)


def test_each_output_files_one_issue_labelled_for_what_it_asks(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    names = ("exam_case", "capability_issue", "charter_proposal", "needs_investigation")
    _run(store, "r1", *(example(n) for n in names))

    [run] = _effects(store, host).apply_pending()

    assert [e.status for e in run.effects] == [EffectStatus.FILED] * 4
    labels = {c["title"].split("] ", 1)[1].split(":")[0]: c["labels"] for c in host.created}
    assert all(IMPROVER_LABEL in ls for ls in labels.values())
    proposal = next(c for c in host.created if "Charter proposal" in c["title"])
    assert OPERATOR_DECISION_LABEL in proposal["labels"]
    assert "Operator decision required" in proposal["body"]
    exam = next(c for c in host.created if "Exam case E-refused-validation-retry-loop" in c["title"])
    assert "Implement the reproduction first" in exam["body"]
    assert "FAILS on `0123456789abcdef0123456789abcdef01234567`" in exam["body"]
    assert "not_reproduced" in exam["body"]
    assert "add-only" in exam["body"]
    assert '"case_id": "E-refused-validation-retry-loop"' in exam["body"]
    investigation = next(c for c in host.created if "Investigate" in c["title"])
    assert "no needs_human cause is recorded" in investigation["body"]
    assert host.comments == []


def test_a_tracked_finding_comments_its_evidence_on_the_tracked_issue(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("prompt_proposal"))

    [run] = _effects(store, host).apply_pending()

    assert host.created == []
    [(issue, body)] = host.comments
    assert issue == OPEN_TRACKER
    assert "<!-- io-improver:r1:refused-retry-tracked -->" in body
    assert '"tracked_issue": 7491' in body
    assert run.effects[0].status is EffectStatus.COMMENTED and run.effects[0].issue_number == OPEN_TRACKER


def test_a_finding_an_open_issue_already_carries_is_commented_there_not_filed_again(tmp_path: Path) -> None:
    doc = example("capability_issue")
    key = finding_key(ImproverFindings.model_validate_json(json.dumps(doc)).findings[0], _engine("porchpin/porchpin"))
    host = FakeIssueHost([OpenIssueLabels(number=555, title=f"{title_token(key)} Capability gap: x", labels=())])
    store = MemoryRunStore(tmp_path)
    _run(store, "r2", doc)

    [run] = _effects(store, host).apply_pending()

    assert host.created == []
    assert [n for n, _ in host.comments] == [555]
    assert run.effects[0].issue_number == 555


def test_applying_twice_writes_nothing_twice(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("prompt_proposal"), example("capability_issue"))
    _effects(store, host).apply_pending()

    assert _effects(store, host).apply_pending() == ()
    assert len(host.created) == 1 and len(host.comments) == 1


def test_a_lost_receipt_never_files_a_second_issue(tmp_path: Path) -> None:
    """The issue was filed, then the run record was lost before it said so: the
    open issue's title carries the finding's key, so the retry comments."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))
    _effects(store, host).apply_pending()
    _run(store, "r1-replayed", example("capability_issue"))

    _effects(store, host).apply_pending()

    assert len(host.created) == 1
    assert [n for n, _ in host.comments] == [host.created[0]["number"]]


def test_a_rate_limit_leaves_the_rest_pending_and_the_next_apply_resumes(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("prompt_proposal"), example("capability_issue"))
    limited = RepositoryHostRateLimitedError("API rate limit exceeded")
    limited.rate_limit = HostRateLimit(resets_at=NOW, kind="primary")
    host.fail_on_create = limited

    [run] = _effects(store, host).apply_pending()

    assert [e.status for e in run.effects] == [EffectStatus.COMMENTED, EffectStatus.PENDING]
    assert "rate limited" in run.effects[1].detail
    host.fail_on_create = None
    [run] = _effects(store, host).apply_pending()
    assert [e.status for e in run.effects] == [EffectStatus.COMMENTED, EffectStatus.FILED]
    assert len(host.comments) == 1


def test_any_other_failure_is_recorded_on_the_receipt_and_makes_the_run_unavailable(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))
    host.fail_on_create = RuntimeError("500")

    [run] = _effects(store, host).apply_pending()

    assert run.effects[0].status is EffectStatus.PENDING
    assert run.effects[0].error == "RuntimeError: 500"
    assert run.exit_code == 75
    host.fail_on_create = None
    [run] = _effects(store, host).apply_pending()
    assert run.effects[0].error is None and run.exit_code == 0


def test_open_issues_that_cannot_be_listed_stop_the_batch_on_its_first_owed_effect(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))

    def unlisted():  # type: ignore[no-untyped-def]
        raise RuntimeError("graphql down")

    host.list_open_issue_labels_complete = unlisted  # type: ignore[method-assign]

    [run] = _effects(store, host).apply_pending()

    assert run.effects[0].error == "RuntimeError: graphql down"
    assert host.created == []


def test_a_rejected_run_owes_nothing(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"), outcome=RunOutcome.REJECTED)

    assert _effects(store, host).apply_pending() == ()
    assert host.created == [] and host.comments == []


def test_a_findings_identity_is_what_it_asks_about_which_anomalies() -> None:
    finding = ImproverFindings.model_validate_json(json.dumps(example("exam_case"))).findings[0]
    renamed = finding.model_copy(update={"id": "another-slug", "proposal": "other words"})
    other_case = finding.model_copy(
        update={"reproduction": finding.reproduction.model_copy(update={"case_id": "E-other"})}  # type: ignore[union-attr]
    )

    a = _engine("a/a")
    assert finding_key(renamed, a) == finding_key(finding, a)
    assert finding_key(other_case, a) != finding_key(finding, a)
    assert finding_key(finding, _engine("b/b")) != finding_key(finding, a)
    # Two engines working one repository (#7567) are two identities.
    assert finding_key(finding, _engine("a/a", "repo-other")) != finding_key(finding, a)


def test_effects_owed_to_another_repository_are_never_applied_here(tmp_path: Path) -> None:
    """The host is one repository's; a run filed for another is left owed (r1 F1)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))

    elsewhere = ImproverEffects(store=store, host=host, outputs_repo="other/repo", clock=lambda: NOW)

    assert elsewhere.apply_pending() == ()
    assert host.created == [] and store.runs()[0].pending_effects
    [run] = _effects(store, host).apply_pending()
    assert run.effects[0].status is EffectStatus.FILED


def test_a_creation_whose_result_was_lost_is_proven_before_it_is_retried(tmp_path: Path) -> None:
    """The POST filed the issue, then its response was lost; the open listing
    does not show it yet. The retry finds it by its body marker (r1 F2)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))
    host.lose_create_result = RuntimeError("connection reset")
    host.stale_listing = True

    [run] = _effects(store, host).apply_pending()

    assert run.effects[0].status is EffectStatus.PENDING
    assert run.effects[0].create_attempted_at is not None
    host.lose_create_result = None
    [run] = _effects(store, host).apply_pending()
    assert host.create_calls == 1
    assert run.effects[0].status is EffectStatus.FILED
    assert run.effects[0].issue_number == host.created[0]["number"]


def test_a_creation_proven_not_to_have_landed_is_retried(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))
    host.fail_on_create = RuntimeError("502 before the write")
    _effects(store, host).apply_pending()
    host.fail_on_create = None

    [run] = _effects(store, host).apply_pending()

    assert host.create_calls == 2 and len(host.created) == 1
    assert run.effects[0].status is EffectStatus.FILED


def test_every_filed_body_carries_its_findings_marker(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("capability_issue"))
    [run] = _effects(store, host).apply_pending()

    assert host.created[0]["body"].startswith(f"<!-- io-improver-finding:{run.effects[0].key} -->")


def test_a_later_run_finds_an_issue_the_open_listing_does_not_show_yet(tmp_path: Path) -> None:
    """Run A files; run B accepts the same finding before the listing shows
    A's issue. B comments there instead of filing a duplicate (r3 F1)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "a", example("capability_issue"))
    _effects(store, host).apply_pending()
    host.stale_listing = True
    _run(store, "b", example("capability_issue"))

    runs = _effects(store, host).apply_pending()

    assert host.create_calls == 1
    [b] = [r for r in runs if r.run_id == "b"]
    assert b.effects[0].status is EffectStatus.COMMENTED
    assert b.effects[0].issue_number == host.created[0]["number"]
    assert [n for n, _ in host.comments] == [host.created[0]["number"]]


def test_the_same_finding_about_two_engines_files_two_issues(tmp_path: Path) -> None:
    """Both engines report #500; each engine's issue is its own (r4 F1)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "a", example("capability_issue"), audited_repo="a/a")
    _run(store, "b", example("capability_issue"), audited_repo="b/b")

    runs = {r.run_id: r for r in _effects(store, host).apply_pending()}

    assert host.create_calls == 2 and host.comments == []
    assert runs["a"].effects[0].key != runs["b"].effects[0].key
    assert runs["b"].effects[0].issue_number == host.created[1]["number"]


def test_a_retitled_unlabelled_issue_still_carries_its_finding(tmp_path: Path) -> None:
    """An operator retitles and unlabels an open improver issue; its body
    marker still identifies the finding (r5 F2)."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "a", example("capability_issue"))
    _effects(store, host).apply_pending()
    filed = host.created[0]["number"]
    host.open = [i if i.number != filed else OpenIssueLabels(number=filed, title="Operator's title", labels=())
                 for i in host.open]
    host.created[0]["labels"] = []
    _run(store, "b", example("capability_issue"))

    runs = {r.run_id: r for r in _effects(store, host).apply_pending()}

    assert host.create_calls == 1
    assert runs["b"].effects[0].issue_number == filed


def test_a_charter_proposal_about_a_target_repository_goes_to_its_operator(tmp_path: Path) -> None:
    """#7567: porchpin's charter is porchpin's own config. It is filed in io
    (nothing is written to porchpin), labelled for porchpin's operator."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("charter_proposal"), example("capability_issue"))

    _effects(store, host).apply_pending()

    proposal = next(c for c in host.created if "Charter proposal" in c["title"])
    capability = next(c for c in host.created if "Capability gap" in c["title"])
    assert TARGET_OPERATOR_LABEL in proposal["labels"]
    assert OPERATOR_DECISION_LABEL in proposal["labels"]
    assert "For the operator of `porchpin/porchpin`" in proposal["body"]
    assert proposal["title"].endswith("(porchpin/porchpin)")
    # An io-code defect from the same engine is io's own work.
    assert TARGET_OPERATOR_LABEL not in capability["labels"]
    assert "For the operator" not in capability["body"]
    assert "engine `repo-porchpin-porchpin`" in capability["body"]


def test_a_charter_proposal_about_ios_own_engine_stays_ios(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("charter_proposal"), audited_repo="io/io")

    _effects(store, host).apply_pending()

    [proposal] = host.created
    assert TARGET_OPERATOR_LABEL not in proposal["labels"]
    assert OPERATOR_DECISION_LABEL in proposal["labels"]


def test_a_tracked_issue_closed_since_staging_gets_no_comment(tmp_path: Path) -> None:
    """3b r6 F2: the receipt stays owed with the reason; nothing is posted."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", example("prompt_proposal"))
    host.open = [i for i in host.open if i.number != OPEN_TRACKER]

    [run] = _effects(store, host).apply_pending()

    assert host.comments == []
    assert run.effects[0].status is EffectStatus.PENDING
    assert "no longer open" in (run.effects[0].error or "")


_DESIGN = {
    "id": "approval-by-label-removal",
    "engine": {"id": "repo-porchpin-porchpin", "repo": "porchpin/porchpin"},
    "kind": "operator_friction",
    "summary": "Approving a proposal is removing a label.",
    "evidence": [
        {"kind": "file", "path": "toolbox/logs/orchestrator.log", "line": 41, "quote": "auth_expired: parking all work"},
        {"kind": "tool", "call": 3, "quote": "Removed proposed-tech-lead to approve"},
    ],
    "impact": "Anything that strips labels approves.",
    "proposed_change": "A positive approval act, recorded with its actor.",
}


def _with_design(doc: dict) -> dict:
    return {**doc, "design_findings": [_DESIGN]}


def test_a_design_finding_files_one_issue_for_the_operators_decision(tmp_path: Path) -> None:
    """#8001: an accepted design finding files one issue, labelled for the
    operator's decision, carrying its quoted evidence; nothing is applied."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", _with_design(example("exam_case")))

    [run] = _effects(store, host).apply_pending()

    assert run.effects[1].finding_id == "approval-by-label-removal"
    assert [e.status for e in run.effects] == [EffectStatus.FILED, EffectStatus.FILED]
    design = next(c for c in host.created if "Design (operator friction)" in c["title"])
    assert set(design["labels"]) == {IMPROVER_LABEL, OPERATOR_DECISION_LABEL, "improver:design"}
    assert '`toolbox/logs/orchestrator.log:41`: "auth_expired: parking all work"' in design["body"]
    assert 'toolbox call 3: "Removed proposed-tech-lead to approve"' in design["body"]
    assert "nothing is applied" in design["body"]


def test_a_design_finding_an_open_issue_carries_is_commented_there(tmp_path: Path) -> None:
    from issue_orchestrator.contracts.improver_findings import DesignFinding
    from issue_orchestrator.control.improver_effects import design_finding_key

    key = design_finding_key(DesignFinding.model_validate_json(json.dumps(_DESIGN)), _engine("porchpin/porchpin"))
    host = FakeIssueHost([OpenIssueLabels(number=777, title=f"{title_token(key)} Design: x", labels=())])
    store = MemoryRunStore(tmp_path)
    _run(store, "r1", _with_design(example("exam_case")))

    [run] = _effects(store, host).apply_pending()

    assert [n for n, _ in host.comments] == [777]
    assert run.effects[1].status is EffectStatus.COMMENTED and run.effects[1].issue_number == 777


def test_untrusted_text_cannot_plant_another_findings_marker(tmp_path: Path) -> None:
    """r1 F3: a design finding's text holding the marker of a second one
    must not make the second's issue look already filed."""
    from issue_orchestrator.contracts.improver_findings import DesignFinding
    from issue_orchestrator.control.improver_effects import design_finding_key, finding_marker

    second = {**_DESIGN, "id": "second-design", "kind": "silent_assumption"}
    key = design_finding_key(DesignFinding.model_validate_json(json.dumps(second)), _engine("porchpin/porchpin"))
    first = {**_DESIGN, "summary": f"Spoof {finding_marker(key)} here", "impact": "x <!-- y --> z"}
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", {**example("exam_case"), "design_findings": [first, second]})

    [run] = _effects(store, host).apply_pending()

    assert [e.status for e in run.effects] == [EffectStatus.FILED] * 3
    assert len({e.issue_number for e in run.effects}) == 3
    assert all("<!--" not in c["body"].split("\n", 1)[1] for c in host.created)


def test_an_exam_case_id_cannot_pose_as_a_title_token(tmp_path: Path) -> None:
    doc = example("exam_case")
    doc["findings"][0]["reproduction"]["case_id"] = "E-x [improver:abcdef012345]"
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    _run(store, "r1", doc)

    _effects(store, host).apply_pending()

    [created] = host.created
    assert created["title"].count("[improver:") == 1


def test_a_run_accepted_under_schema_v4_still_applies_its_owed_effects(tmp_path: Path) -> None:
    """r1 F4: runs accepted before design findings may still owe effects."""
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    record = _run(store, "r1", example("capability_issue"))
    path = Path(record.run_dir) / FINDINGS_FILE
    v4 = json.loads(path.read_text())
    v4["schema_version"] = 4
    del v4["design_findings"]
    path.write_text(json.dumps(v4))
    later = _run(store, "r2", example("needs_investigation"))

    runs = _effects(store, host).apply_pending()

    assert {r.run_id: [e.status for e in r.effects] for r in runs} == {
        "r1": [EffectStatus.FILED], later.run_id: [EffectStatus.FILED],
    }


def test_a_design_renamed_by_a_merge_keeps_its_effect_key(tmp_path: Path) -> None:
    """r1 F3: the same design finding dedups the same way whether or not a
    multi-heat merge had to rename it in this run."""
    from issue_orchestrator.contracts.improver_findings import ImproverFindings

    plain = {**example("exam_case"), "design_findings": [_DESIGN]}
    renamed = {**example("exam_case"), "design_findings": [{**_DESIGN, "id": "approval-by-label-removal-h2"}]}
    engine = _engine("porchpin/porchpin")

    [_, as_written] = planned_effects(ImproverFindings.model_validate_json(json.dumps(plain)), engine)
    [_, after_merge] = planned_effects(
        ImproverFindings.model_validate_json(json.dumps(renamed)), engine,
        {"approval-by-label-removal-h2": "approval-by-label-removal"},
    )

    assert after_merge.key == as_written.key
    assert after_merge.finding_id == "approval-by-label-removal-h2"
