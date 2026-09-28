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
    finding_key,
    planned_effects,
    title_token,
)
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.domain.host_rate_limit import HostRateLimit
from issue_orchestrator.ports.engine_audit import OpenIssueLabels
from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError
from tests.unit.improver_support import OPEN_TRACKER, FakeIssueHost, MemoryRunStore, example

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


def _run(store: MemoryRunStore, run_id: str, *docs: dict, outcome: RunOutcome = RunOutcome.ACCEPTED) -> ImproverRunRecord:
    merged = json.loads(json.dumps(docs[0]))
    merged["findings"] = [f for d in docs for f in d["findings"]]
    findings = ImproverFindings.model_validate_json(json.dumps(merged))
    run_dir = store.new_run_dir(run_id)
    (run_dir / FINDINGS_FILE).write_text(json.dumps(merged))
    record = ImproverRunRecord(
        run_id=run_id, started_at=NOW, finished_at=NOW, outcome=outcome, detail="",
        audited_repo="porchpin/porchpin", outputs_repo="io/io", run_dir=str(run_dir),
        engine_commit=merged["engine_commit"],
        effects=planned_effects(findings) if outcome is RunOutcome.ACCEPTED else (),
    )
    store.record(record)
    return record


def _effects(store: MemoryRunStore, host: FakeIssueHost) -> ImproverEffects:
    return ImproverEffects(store=store, host=host, clock=lambda: NOW)


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
    assert "GitHub label events are not staged" in investigation["body"]
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
    key = finding_key(ImproverFindings.model_validate_json(json.dumps(doc)).findings[0])
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

    assert finding_key(renamed) == finding_key(finding)
    assert finding_key(other_case) != finding_key(finding)
