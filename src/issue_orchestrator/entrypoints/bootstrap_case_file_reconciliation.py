"""Compose the bounded reconciliation command using normal lifecycle owners."""

from typing import TYPE_CHECKING

from ..control.tech_lead_case_file_reconciliation import CaseFileReconciliationHost
from ..control.tech_lead_case_file_lifecycle_reconciliation import (
    CaseFileLifecycleReconciler,
)
from ..execution.case_file_reconciliation_adapter import CaseFileReconciliationAdapter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..control.action_results import ActionResult
    from ..control.actions import Action
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from ..ports import RepositoryHost


def build_case_file_reconciliation_host(
    orchestrator: "Orchestrator",
) -> CaseFileReconciliationHost:
    """Keep repository/ledger/applier wiring inside the composition root."""
    deps = orchestrator.deps
    return CaseFileReconciliationAdapter(
        list_pattern_evidence=deps.services.tech_lead_authority.list_pattern_evidence,
        get_issue_state=deps.repository_host.get_issue_state,
        apply_all=deps.action_applier.apply_all,
    )


def build_case_file_reconciliation_preview_host(
    config: "Config",
) -> CaseFileReconciliationHost:
    """Read-only evidence-plan host for a dry run (#7248 review F4).

    The evidence branch used to build the whole orchestrator regardless of
    ``--apply`` and then pass ``apply_writes=False`` to the runner. By then the
    composition had already constructed and migrated the local SQLite authority
    store and, where shared pattern authority is configured, seeded and mirrored
    it to the durable GitHub ref. A command that promises to write nothing had
    therefore already written, and the test that covered it injected an
    already-built host, so it proved only that the applier was not called.

    This is the same split the lifecycle branch makes: a DIFFERENT composition
    for the preview rather than the write-capable one with its writes
    suppressed. It builds no orchestrator, initializes no schema, and its
    ``apply`` refuses rather than trusting the runner to stay correct.
    """
    from ..execution.providers import create_repository_host
    from ..infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore

    if config.repo is None:
        raise ValueError("the configured repository could not be resolved")
    repository_host = create_repository_host(config.repo, config)
    # Deliberately NOT initialize()d: a dry run reads the ledger a previous run
    # left, and must not create or migrate one. A missing store fails the read
    # loudly, which is the correct answer to "reconcile a ledger that is not
    # there".
    store = SqliteTechLeadAuthorityStore.for_repo(config.repo_root)

    def _refuse(actions: "Sequence[Action]") -> "Sequence[ActionResult]":
        raise RuntimeError(
            f"an evidence-plan dry run may not apply {len(actions)} action(s);"
            " re-run with --apply"
        )

    return CaseFileReconciliationAdapter(
        list_pattern_evidence=store.list_pattern_evidence,
        get_issue_state=repository_host.get_issue_state,
        apply_all=_refuse,
    )


def build_case_file_lifecycle_reconciler(
    config: "Config",
    *,
    apply_writes: bool,
) -> CaseFileLifecycleReconciler:
    """Compose durable lifecycle authority without starting background services.

    ``apply_writes`` selects between two DIFFERENT compositions, not one
    composition with a suppressed write. A preview gets read-only shared
    authority and no mutation authority at all, so nothing it can reach — not
    the local SQLite authority store, not the shared registry, not GitHub — is
    writable; an apply gets the write-through mirror and the reconciliation
    gate. Suppressing a single write on a write-capable composition was not
    enough: reads through the mirror still migrated local authority and
    discarded pending intents from a command that promised to write nothing
    (#7248 review F1/A1).
    """
    from ..execution.providers import create_repository_host

    if config.repo is None:
        raise ValueError("the configured repository could not be resolved")
    repository_host = create_repository_host(config.repo, config)
    if not apply_writes:
        return _build_lifecycle_preview(config, repository_host)
    return _build_lifecycle_applier(config, repository_host)


def _build_lifecycle_preview(
    config: "Config", repository_host: "RepositoryHost"
) -> CaseFileLifecycleReconciler:
    """Read-only lifecycle authority for a dry run."""
    from ..control.tech_lead_case_file_lifecycle_reconciliation import (
        CaseFileLifecycleReconciliationRefused,
    )
    from .bootstrap_tech_lead import create_pattern_registry_preview

    class _RefusingAuthority:
        """Grants nothing, for either kind of write.

        Unreachable while the reconciler only applies when asked to, and wired
        anyway: the preview's guarantee must not rest on that caller staying
        correct. The registry refuses the write on its own, and this refuses the
        authority to attempt one.
        """

        def require_retirement(self, issue_number: int) -> None:
            self._refuse(issue_number)

        def require_classification(self, issue_number: int) -> None:
            self._refuse(issue_number)

        @staticmethod
        def _refuse(issue_number: int) -> None:
            raise CaseFileLifecycleReconciliationRefused(
                f"a lifecycle dry run may not mutate #{issue_number};"
                " re-run with --apply"
            )

    return CaseFileLifecycleReconciler(
        registry=create_pattern_registry_preview(config, repository_host),
        repository_host=repository_host,
        mutation_authority=_RefusingAuthority(),
    )


def _build_lifecycle_applier(
    config: "Config", repository_host: "RepositoryHost"
) -> CaseFileLifecycleReconciler:
    """Write-capable lifecycle authority behind the reconciliation gate."""
    from ..control.label_manager import LabelManager
    from ..control.mutation_gate import ReconciliationGate
    from ..control.tech_lead_case_file_lifecycle_reconciliation import (
        GatedCaseFileMutationAuthority,
    )
    from ..execution.providers import create_fresh_issue_reader
    from .bootstrap_tech_lead import (
        create_pattern_registry,
        create_tech_lead_authority_store,
    )

    assert config.repo is not None
    registry = create_pattern_registry(
        config,
        repository_host,
        create_tech_lead_authority_store(config),
        shared_required=True,
    )
    # ONE reader, wired as both ports. A nonterminal outcome's authority
    # constrains the issue's own state, and the gate refuses an expectation it
    # has no snapshot reader for rather than narrowing it to labels — so
    # omitting this argument would fail every classification closed instead of
    # quietly downgrading it (#7248 round 6 review F8/A3).
    fresh = create_fresh_issue_reader(config.repo, config)
    return CaseFileLifecycleReconciler(
        registry=registry,
        repository_host=repository_host,
        mutation_authority=GatedCaseFileMutationAuthority(
            gate=ReconciliationGate(
                fresh_issue_reader=fresh,
                reconcile=True,
                fresh_issue_snapshot_reader=fresh,
            ),
            pause_label=LabelManager(config).needs_reconcile,
        ),
    )
