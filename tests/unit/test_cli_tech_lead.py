"""Tests for the on-demand tech_lead CLI helpers.

The reconciliation command is the CONSUMER half of the case-file reconciliation
boundary (its planner half lives in
``tests/unit/control/test_tech_lead_case_file_reconciliation.py``). Both halves
are covered because this command closes issues on a live board: a wrong deps
path, a mis-spelled issue state, or an unhandled reconciliation exception would
otherwise only surface during a real one-shot run.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from issue_orchestrator.control.actions import (
    ActionResult,
    CloseIssueAction,
    CreateTechLeadCaseFileIssueAction,
)
from issue_orchestrator.control.claim_gate import ClaimLostError
from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
)
from issue_orchestrator.control.tech_lead_case_file_reconciliation import (
    CaseFileReconciliationPlan,
)
from issue_orchestrator.domain.tech_lead_findings import PatternEvidence
from issue_orchestrator.execution.case_file_reconciliation_adapter import CaseFileReconciliationAdapter
from issue_orchestrator.entrypoints.bootstrap_case_file_reconciliation import build_case_file_reconciliation_host
from issue_orchestrator.entrypoints.cli_tech_lead import (
    load_reconciliation_plan,
    run_case_file_reconciliation,
)
from issue_orchestrator.infra.config import Config

VALID_PLAN = """
plan_id: "test-plan"
clusters:
  - signature: some-recurring-class
    tracker: 100
    summary: What the class is.
    duplicates:
      - issue: 101
        note: A re-sighting.
"""


# --- plan loading ----------------------------------------------------------


def test_valid_plan_loads(tmp_path: Path):
    path = tmp_path / "plan.yaml"
    path.write_text(VALID_PLAN, encoding="utf-8")

    plan = load_reconciliation_plan(path)

    assert plan.plan_id == "test-plan"
    assert plan.clusters[0].duplicate_issue_numbers == (101,)


def test_malformed_yaml_is_reported_as_an_invalid_plan(tmp_path: Path):
    """A syntax error is a bad plan, not an unhandled yaml exception."""
    path = tmp_path / "plan.yaml"
    path.write_text('plan_id: "unterminated\n  x: [\n', encoding="utf-8")

    with pytest.raises(ValueError, match="not valid YAML"):
        load_reconciliation_plan(path)


@pytest.mark.parametrize(
    "document", ["- a\n- list\n", "just a scalar\n", ""], ids=["list", "scalar", "empty"]
)
def test_a_document_that_is_not_a_plan_is_rejected(tmp_path: Path, document: str):
    path = tmp_path / "plan.yaml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match="must be a mapping"):
        load_reconciliation_plan(path)


def test_a_missing_plan_file_raises_oserror(tmp_path: Path):
    with pytest.raises(OSError):
        load_reconciliation_plan(tmp_path / "absent.yaml")


# --- the orchestrator-bound execution boundary -----------------------------


def _config() -> Config:
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.filtering.label = "io-scope"
    return config


class _RecordingApplier:
    """Stands in for ActionApplier.apply_all at the same contract."""

    def __init__(self, *, raises=None, fail_all=False):
        self.applied: list[Any] = []
        self.calls = 0
        self._raises = raises
        self._fail_all = fail_all

    def __call__(self, actions):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        self.applied.extend(actions)
        return [
            ActionResult.fail(action, "boom")
            if self._fail_all
            else ActionResult.ok(action)
            for action in actions
        ]


def _host(*, rows=(), states=None, applier=None) -> CaseFileReconciliationAdapter:
    issue_states = dict(states or {})
    return CaseFileReconciliationAdapter(
        list_pattern_evidence=lambda: tuple(rows),
        get_issue_state=issue_states.get,
        apply_all=applier or _RecordingApplier(),
    )


def _row(signature="some-recurring-class", issue=7100) -> PatternEvidence:
    return PatternEvidence(
        signature=signature, case_file_issue_number=issue, observation_count=1
    )


def _plan() -> CaseFileReconciliationPlan:
    return CaseFileReconciliationPlan.from_mapping(
        {
            "plan_id": "test-plan",
            "clusters": [
                {
                    "signature": "some-recurring-class",
                    "tracker": 100,
                    "summary": "What the class is.",
                    "duplicates": [{"issue": 101, "note": "A re-sighting."}],
                }
            ],
        }
    )


def test_host_projects_the_authority_ledger_by_signature():
    ledger = _host(rows=(_row(),)).pattern_ledger()

    assert ledger["some-recurring-class"].case_file_issue_number == 7100


def test_host_reads_issue_state_and_only_open_counts():
    host = _host(states={101: "open", 102: "closed"})

    assert host.issue_is_open(101) is True
    assert host.issue_is_open(102) is False
    # An unknown issue (deleted/transferred) is not open, and never assumed to be.
    assert host.issue_is_open(999) is False


def test_host_applies_through_the_applier_and_skips_the_empty_call():
    applier = _RecordingApplier()
    host = _host(applier=applier)

    assert host.apply(()) == ()
    assert applier.calls == 0

    action = CloseIssueAction(issue_number=101, reason="r")
    results = host.apply([action])

    assert applier.applied == [action]
    assert [result.success for result in results] == [True]


def test_host_binds_to_a_live_orchestrators_dependencies():
    """The composition root binds the real owner ports."""

    class _Authority:
        def list_pattern_evidence(self):
            return (_row(),)

    class _RepositoryHost:
        def get_issue_state(self, issue_number):
            return "open" if issue_number == 101 else "closed"

    applier = _RecordingApplier()
    orchestrator = SimpleNamespace(
        config=_config(),
        deps=SimpleNamespace(
            services=SimpleNamespace(tech_lead_authority=_Authority()),
            repository_host=_RepositoryHost(),
            action_applier=SimpleNamespace(apply_all=applier),
        ),
    )

    host = build_case_file_reconciliation_host(orchestrator)  # type: ignore[arg-type]

    assert host.pattern_ledger()["some-recurring-class"].case_file_issue_number == 7100
    assert host.issue_is_open(101) is True
    assert host.issue_is_open(102) is False
    host.apply([CloseIssueAction(issue_number=101, reason="r")])
    assert applier.calls == 1


# --- the command's exit-code contract --------------------------------------


def test_dry_run_writes_nothing_and_exits_zero(capsys):
    applier = _RecordingApplier()
    host = _host(applier=applier, states={101: "open"})

    code = run_case_file_reconciliation(
        _plan(), host, config=_config(), apply_writes=False
    )

    assert code == 0
    assert applier.calls == 0
    out = capsys.readouterr().out
    assert "Dry run" in out
    # The closure it cannot plan yet is still shown, so the operator can read
    # the whole intended outcome before --apply.
    assert "#101" in out


def test_apply_runs_both_phases_and_exits_zero(capsys):
    applier = _RecordingApplier()
    host = _host(rows=(_row(),), applier=applier, states={101: "open"})

    code = run_case_file_reconciliation(
        _plan(), host, config=_config(), apply_writes=True
    )

    assert code == 0
    assert any(isinstance(action, CloseIssueAction) for action in applier.applied)
    assert "Reconciliation complete" in capsys.readouterr().out


def test_failed_evidence_exits_one_and_reports_that_nothing_was_closed(capsys):
    applier = _RecordingApplier(fail_all=True)
    host = _host(applier=applier, states={101: "open"})

    code = run_case_file_reconciliation(
        _plan(), host, config=_config(), apply_writes=True
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Stopped before any issue was closed" in out
    # The failure branch renders the offending action and its error.
    assert "boom" in out
    assert not any(isinstance(action, CloseIssueAction) for action in applier.applied)
    assert any(
        isinstance(action, CreateTechLeadCaseFileIssueAction)
        for action in applier.applied
    )


@pytest.mark.parametrize(
    "error",
    [
        ReconciliationRequired(
            entity_type="issue",
            entity_id=101,
            expected=ExternalSnapshot.for_issue(101, set()),
            actual=ExternalSnapshot.for_issue(101, {"io:needs-reconcile"}),
            reason="paused for human reconciliation",
        ),
        ClaimLostError(issue_number=101, operation="close_issue"),
    ],
    ids=["paused", "claim-lost"],
)
def test_a_gate_rejection_is_reported_not_a_traceback(capsys, error):
    """The applier re-raises these past apply_all; the command must catch them."""
    host = _host(applier=_RecordingApplier(raises=error), states={101: "open"})

    code = run_case_file_reconciliation(
        _plan(), host, config=_config(), apply_writes=True
    )

    assert code == 1
    assert "Reconciliation halted" in capsys.readouterr().out

