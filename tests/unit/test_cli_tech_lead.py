"""Tests for the on-demand tech_lead CLI helpers.

The reconciliation command is the CONSUMER half of the case-file reconciliation
boundary (its planner half lives in
``tests/unit/control/test_tech_lead_case_file_reconciliation.py``). Both halves
are covered because this command closes issues on a live board: a wrong deps
path, a mis-spelled issue state, or an unhandled reconciliation exception would
otherwise only surface during a real one-shot run.
"""

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
from issue_orchestrator.control.tech_lead_case_file_lifecycle_reconciliation import (
    CaseFileLifecycleReconciliationPlan,
    CaseFileLifecycleReconciliationRefused,
    CaseFileLifecycleReconciliationResult,
)
from issue_orchestrator.domain.tech_lead_findings import PatternEvidence
from issue_orchestrator.execution.case_file_reconciliation_adapter import (
    CaseFileReconciliationAdapter,
)
from issue_orchestrator.entrypoints.bootstrap_case_file_reconciliation import (
    build_case_file_reconciliation_host,
)
from issue_orchestrator.entrypoints.cli_tech_lead import (
    load_reconciliation_plan,
    run_case_file_reconciliation,
    run_case_file_lifecycle_reconciliation,
)
from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.infra.config import Config

from tests.unit.adapters.github.test_ref_claim_adapter import FakeGitHubRefClient

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

VALID_LIFECYCLE_PLAN = """
plan_id: "lifecycle-test-plan"
repository: owner/repo
recorded_at: "2026-09-10T12:00:00+00:00"
outcomes:
  - signature: some-recurring-class
    issue: 100
    disposition: shipped
    expected_revision: "0000000000000000000000000000000000000000000000000000000000000000"
    reason: The underlying fix shipped.
    evidence:
      - https://example.test/pull/1
"""


# --- plan loading ----------------------------------------------------------


def test_valid_plan_loads(tmp_path: Path):
    path = tmp_path / "plan.yaml"
    path.write_text(VALID_PLAN, encoding="utf-8")

    plan = load_reconciliation_plan(path)

    assert plan.plan_id == "test-plan"
    assert plan.clusters[0].duplicate_issue_numbers == (101,)


def test_lifecycle_plan_loads_through_the_same_command_surface(tmp_path: Path):
    path = tmp_path / "lifecycle.yaml"
    path.write_text(VALID_LIFECYCLE_PLAN, encoding="utf-8")

    plan = load_reconciliation_plan(path)

    assert isinstance(plan, CaseFileLifecycleReconciliationPlan)
    assert plan.repository == "owner/repo"
    assert plan.outcomes[0].disposition == "shipped"


def test_malformed_yaml_is_reported_as_an_invalid_plan(tmp_path: Path):
    """A syntax error is a bad plan, not an unhandled yaml exception."""
    path = tmp_path / "plan.yaml"
    path.write_text('plan_id: "unterminated\n  x: [\n', encoding="utf-8")

    with pytest.raises(ValueError, match="not valid YAML"):
        load_reconciliation_plan(path)


@pytest.mark.parametrize(
    "document",
    ["- a\n- list\n", "just a scalar\n", ""],
    ids=["list", "scalar", "empty"],
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


class _LifecycleReconciler:
    def __init__(self, result: CaseFileLifecycleReconciliationResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    def run(self, plan, *, configured_repository, apply_writes):
        self.calls.append((configured_repository, apply_writes))
        return self.result


def _lifecycle_plan() -> CaseFileLifecycleReconciliationPlan:
    import yaml

    return CaseFileLifecycleReconciliationPlan.from_mapping(
        yaml.safe_load(VALID_LIFECYCLE_PLAN)
    )


def test_lifecycle_dry_run_renders_review_counts_and_writes_nothing(capsys):
    reconciler = _LifecycleReconciler(
        CaseFileLifecycleReconciliationResult(
            plan_id="lifecycle-test-plan",
            dry_run=True,
            active=0,
            needs_human=0,
            terminal=1,
            applied=0,
        )
    )

    code = run_case_file_lifecycle_reconciliation(
        _lifecycle_plan(),
        reconciler,  # type: ignore[arg-type]
        configured_repository="owner/repo",
        apply_writes=False,
    )

    assert code == 0
    assert reconciler.calls == [("owner/repo", False)]
    assert "1 terminal" in capsys.readouterr().out


def test_lifecycle_failure_is_a_nonzero_bounded_stop(capsys):
    reconciler = _LifecycleReconciler(
        CaseFileLifecycleReconciliationResult(
            plan_id="lifecycle-test-plan",
            dry_run=False,
            active=0,
            needs_human=0,
            terminal=1,
            applied=0,
            failures=("some-recurring-class: ambiguous write",),
        )
    )

    code = run_case_file_lifecycle_reconciliation(
        _lifecycle_plan(),
        reconciler,  # type: ignore[arg-type]
        configured_repository="owner/repo",
        apply_writes=True,
    )

    assert code == 1
    assert reconciler.calls == [("owner/repo", True)]
    assert "ambiguous write" in capsys.readouterr().out


def test_lifecycle_refusal_is_reported_as_a_bounded_nonzero_stop(capsys):
    """Including the one the owner raises for a failed shared-authority read.

    ``list_entries`` is a live GitHub-ref read that validation performs after
    composition, so an operational failure in that window used to escape the
    supported command as a traceback. The command now renders the owner's one
    typed refusal and exits 1 (#7248 review F3).
    """

    class _Refusing:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def run(self, plan, *, configured_repository, apply_writes):
            self.calls.append((configured_repository, apply_writes))
            raise CaseFileLifecycleReconciliationRefused(
                "shared pattern authority could not be read: registry ref 503"
            )

    reconciler = _Refusing()

    code = run_case_file_lifecycle_reconciliation(
        _lifecycle_plan(),
        reconciler,  # type: ignore[arg-type]
        configured_repository="owner/repo",
        apply_writes=True,
    )

    assert code == 1
    assert reconciler.calls == [("owner/repo", True)]
    out = capsys.readouterr().out
    assert "Lifecycle reconciliation refused" in out
    assert "shared pattern authority could not be read" in out


# --- lifecycle preview composition (#7248 review F1) ------------------------


def _shared_registry(client) -> "GitHubRefPatternRegistry":
    from issue_orchestrator.adapters.github.pattern_registry import (
        GitHubRefPatternRegistry,
    )

    return GitHubRefPatternRegistry(
        cast(Any, client), claimant_id="engine-a", lease_seconds=30
    )


def _seed_shared_case_file(client) -> str:
    """Commit exactly one shared row, and return its reviewed revision."""
    from issue_orchestrator.domain.tech_lead_findings import PendingCaseFile

    registry = _shared_registry(client)
    reserved = registry.reserve(
        PendingCaseFile(
            signature="narrow",
            title="Pattern case file: narrow",
            idempotency_marker="<!-- marker:narrow -->",
            body_observation_id="run:a:A1",
            fix_class="code",
            area="runtime",
            diagnosis="Shared authority knows only this row.",
        )
    )
    entry = registry.finalize(
        signature="narrow",
        reservation_id=reserved.entry.reservation_id,
        issue_number=1,
    )
    return entry.review_revision()


def _seed_local_authority(repo_root: Path) -> None:
    """Rolling-upgrade local state: richer than shared, plus a pending intent."""
    from issue_orchestrator.domain.tech_lead_findings import PendingCaseFile
    from issue_orchestrator.infra.tech_lead_authority_store import (
        SqliteTechLeadAuthorityStore,
    )

    store = SqliteTechLeadAuthorityStore.for_repo(repo_root)
    store.record_pattern(
        signature="narrow", issue_number=1, observation_id="run:a:A1"
    )
    store.note_pattern_observation(signature="narrow", observation_id="run:a:A2")
    store.record_pattern(
        signature="local-only", issue_number=2, observation_id="run:b:B1"
    )
    store.record_pending_case_file(
        pending=PendingCaseFile(
            signature="narrow",
            title="Pattern case file: narrow",
            idempotency_marker="<!-- marker:narrow -->",
            body_observation_id="run:a:A3",
            fix_class="code",
            area="runtime",
            diagnosis="An interrupted create nobody has finished yet.",
        )
    )


def _local_authority_state(repo_root: Path) -> dict[str, object]:
    from issue_orchestrator.infra.tech_lead_authority_store import (
        SqliteTechLeadAuthorityStore,
    )

    store = SqliteTechLeadAuthorityStore.for_repo(repo_root)
    return {
        "patterns": store.list_patterns(),
        "narrow_observations": store.list_pattern_observation_ids(
            signature="narrow"
        ),
        "local_only_observations": store.list_pattern_observation_ids(
            signature="local-only"
        ),
        "pending": store.load_pending_case_file(signature="narrow"),
    }


def _lifecycle_config(tmp_path: Path):
    config = Config(repo_root=tmp_path)
    config.repo = "owner/repo"
    config.tech_lead_enabled = False
    return config


def test_lifecycle_dry_run_leaves_shared_and_local_authority_untouched(
    tmp_path: Path, monkeypatch
):
    """A dry run may not rewrite the authority it is previewing.

    Composition used to suppress only the rolling-upgrade seed and then read
    through the write-through mirror, whose ``list_entries`` migrates every
    committed shared row into local SQLite and discards any matching pending
    create intent. With shared authority narrower than local state, previewing
    therefore replaced local evidence with the shared snapshot and destroyed a
    pending intent — from the command that prints "nothing was written" (#7248
    review F1/A1).
    """
    from issue_orchestrator.entrypoints import bootstrap_case_file_reconciliation
    from issue_orchestrator.execution import providers

    client = FakeGitHubRefClient()
    revision = _seed_shared_case_file(client)
    _seed_local_authority(tmp_path)
    before_local = _local_authority_state(tmp_path)
    before_refs = dict(client.refs)
    monkeypatch.setattr(
        providers,
        "create_repository_host",
        lambda repo, config: GitHubAdapter(repo=repo, http_client=cast(Any, client)),
    )
    plan = CaseFileLifecycleReconciliationPlan.from_mapping(
        {
            "plan_id": "preview-plan",
            "repository": "owner/repo",
            "recorded_at": "2026-09-10T12:00:00+00:00",
            "outcomes": [
                {
                    "signature": "narrow",
                    "issue": 1,
                    "disposition": "shipped",
                    "expected_revision": revision,
                    "reason": "The underlying fix shipped.",
                    "evidence": ["owner/repo#1"],
                }
            ],
        }
    )

    reconciler = bootstrap_case_file_reconciliation.build_case_file_lifecycle_reconciler(
        _lifecycle_config(tmp_path), apply_writes=False
    )
    result = reconciler.run(
        plan, configured_repository="owner/repo", apply_writes=False
    )

    assert result.dry_run and result.terminal == 1 and result.applied == 0
    assert dict(client.refs) == before_refs
    assert _local_authority_state(tmp_path) == before_local
    assert before_local["narrow_observations"] == ("run:a:A1", "run:a:A2")
    assert before_local["pending"] is not None


def test_lifecycle_dry_run_does_not_create_the_local_authority_store(
    tmp_path: Path, monkeypatch
):
    """Preview must not so much as initialize the writable local database."""
    from issue_orchestrator.entrypoints import bootstrap_case_file_reconciliation
    from issue_orchestrator.execution import providers
    from issue_orchestrator.infra.repo_identity import state_dir

    client = FakeGitHubRefClient()
    monkeypatch.setattr(
        providers,
        "create_repository_host",
        lambda repo, config: GitHubAdapter(repo=repo, http_client=cast(Any, client)),
    )

    bootstrap_case_file_reconciliation.build_case_file_lifecycle_reconciler(
        _lifecycle_config(tmp_path), apply_writes=False
    )

    assert not (state_dir(tmp_path) / "tech_lead_authority.sqlite").exists()


def test_lifecycle_command_composes_read_only_authority_without_apply(
    tmp_path: Path, monkeypatch, capsys
):
    """``--apply`` selects the composition, not just what the reconciler does."""
    from issue_orchestrator.entrypoints import (
        bootstrap_case_file_reconciliation,
        cli_tech_lead,
    )

    path = tmp_path / "lifecycle.yaml"
    path.write_text(VALID_LIFECYCLE_PLAN, encoding="utf-8")
    config = _lifecycle_config(tmp_path)
    selected: list[bool] = []

    def _build(config_arg, *, apply_writes):
        selected.append(apply_writes)
        return _LifecycleReconciler(
            CaseFileLifecycleReconciliationResult(
                plan_id="lifecycle-test-plan",
                dry_run=not apply_writes,
                active=0,
                needs_human=0,
                terminal=1,
                applied=0,
            )
        )

    monkeypatch.setattr(cli_tech_lead, "load_config", lambda args: config)
    monkeypatch.setattr(
        bootstrap_case_file_reconciliation,
        "build_case_file_lifecycle_reconciler",
        _build,
    )

    for apply in (False, True):
        code = cli_tech_lead.cmd_reconcile_case_files(
            argparse.Namespace(plan=str(path), apply=apply)
        )
        assert code == 0

    assert selected == [False, True]
    capsys.readouterr()
