"""Composition root for retained-work admission, custody, and recovery."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..infra.config import Config
from ..ports.command_runner import CommandRunner
from ..ports.exact_git import ExactGit
from ..ports.completion_intake import CompletionIntakeLedger
from ..control.validated_work_admission import RankedEvidenceAdmission
from ..ports.validated_work_store import ValidatedWorkStore
from ..ports.validated_work_recovery_store import ValidatedWorkRecoveryStore
from ..control.validated_work_escrow import ValidatedWorkEscrowMaintenance
from ..ports.issue_disposition_gate import IssueDispositionMutationGate
from ..ports.publication_workspace import PublicationWorkspaces
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_escrow import ValidatedWorkEscrow
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner
from ..ports.validated_work_verification import OrchestratorLivenessPort
from ..ports.validated_work_capture_observer import (
    UnavailableValidatedWorkCaptureObserver, ValidatedWorkCaptureObserver,
)

if TYPE_CHECKING:
    from ..control.action_applier import ActionApplier
    from ..control.aggregate_recovery_block import AggregateRecoveryBlocks
    from ..control.completion_processor import CompletionProcessor
    from ..control.label_manager import LabelManager
    from ..control.needs_human_block import SharedNeedsHumanBlock
    from ..control.recovery_drain import RecoveryDrain
    from ..control.review_exchange_lifecycle import CoreIssueRuntimeOwners
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.publication_remote import PublicationRemote
    from ..ports.recovery_issue_reader import RecoveryIssueReader
    from ..execution.git_working_copy import GitWorkingCopy
    from ..execution.validated_work_adapters import ValidatedWorkRepositoryHost


def build_validated_work_escrow_maintenance(
    config: Config,
    *,
    store: ValidatedWorkStore,
    working_copy: ExactGit,
) -> ValidatedWorkEscrowMaintenance:
    """Slice-2 composition seam; the slice-3 disposition owner schedules it."""
    from ..infra.validated_work_escrow import FilesystemValidatedWorkEscrow

    if config.repo is None:
        raise ValueError("escrow requires the configured repository identity")
    escrow = FilesystemValidatedWorkEscrow(
        config.repo_root / ".issue-orchestrator" / "state" / "validated-work",
        repository=config.repo_root,
        repo_slug=config.repo,
        git=working_copy,
    )
    return ValidatedWorkEscrowMaintenance(
        escrow=escrow,
        store=store,
        retention_days=config.validated_work.escrow_retention_days,
    )


from ..control.validated_work_capture import ValidatedWorkCustody
from ..control.validated_work_escrow import EscrowReconciliation
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore


@dataclass(frozen=True, slots=True)
class ValidatedWorkAdmissionOwners:
    store: ValidatedWorkAdmissionStore
    custody: ValidatedWorkCustody
    repair: EscrowReconciliation
    capture_observer: ValidatedWorkCaptureObserver


@dataclass(frozen=True, slots=True)
class ValidatedWorkRecoveryOwners(ValidatedWorkAdmissionOwners):
    """One process-scoped ownership graph shared by admission and recovery."""

    records: ValidatedWorkRecoveryStore
    intake: CompletionIntakeLedger
    escrow: ValidatedWorkEscrow
    gate: IssueDispositionMutationGate
    execution: ValidatedWorkExecutionOwner
    effects: ValidatedWorkEffectAuthority
    blocks: "AggregateRecoveryBlocks"
    workspaces: PublicationWorkspaces
    remote: "PublicationRemote"
    issues: "RecoveryIssueReader"


def build_validated_work_admission(config: Config, working_copy: ExactGit, intake: CompletionIntakeLedger) -> ValidatedWorkAdmissionOwners:
    from ..infra.repo_identity import state_dir
    from ..infra.validated_work_escrow import FilesystemValidatedWorkEscrow
    from ..infra.validated_work_intake_store import SqliteValidatedWorkIntakeStore
    from ..execution.validated_work_ancestry import GitValidatedWorkAncestry

    if config.repo is None:
        raise ValueError("validated work requires configured repository identity")
    root = state_dir(config.repo_root)
    escrow = FilesystemValidatedWorkEscrow(root / "validated-work", repository=config.repo_root, repo_slug=config.repo, git=working_copy)
    ancestry = GitValidatedWorkAncestry(repository=config.repo_root, repo_slug=config.repo, git=working_copy)
    store = RankedEvidenceAdmission(SqliteValidatedWorkIntakeStore(root / "validated_work.sqlite", ancestry, escrow), intake)
    return ValidatedWorkAdmissionOwners(
        store, ValidatedWorkCustody(escrow, store),
        EscrowReconciliation(escrow=escrow, store=store),
        UnavailableValidatedWorkCaptureObserver(),
    )


def build_validated_work_runtime(
    config: Config,
    working_copy: "GitWorkingCopy",
    intake: CompletionIntakeLedger,
    command_runner: CommandRunner,
    liveness: OrchestratorLivenessPort,
    repository_host: "ValidatedWorkRepositoryHost",
    fresh_issue_reader: "FreshIssueReader",
    action_applier: "ActionApplier",
    label_manager: "LabelManager",
    human_block: "SharedNeedsHumanBlock",
) -> ValidatedWorkRecoveryOwners:
    """Build the full store once, then put aggregate policy around admission."""
    from ..control.aggregate_recovery_block import AggregateRecoveryBlocks
    from ..control.validated_work_effects import FencedValidatedWorkEffects
    from ..execution.publication_workspace import EscrowPublicationWorkspaces
    from ..execution.git_tools import create_git
    from ..execution.validated_work_ancestry import GitValidatedWorkAncestry
    from ..execution.validated_work_execution import LocalValidatedWorkExecutionOwner
    from ..execution.validated_work_adapters import build_validated_work_external_adapters
    from ..infra.repo_identity import state_dir
    from ..infra.validated_work_escrow import FilesystemValidatedWorkEscrow
    from ..infra.validated_work_store import SqliteValidatedWorkStore

    if config.repo is None:
        raise ValueError("validated work requires configured repository identity")
    git = create_git(command_runner)
    root = state_dir(config.repo_root)
    external = build_validated_work_external_adapters(
        repo_root=root, repo_slug=config.repo, repository_host=repository_host
    )
    escrow = FilesystemValidatedWorkEscrow(
        root / "validated-work",
        repository=config.repo_root,
        repo_slug=config.repo,
        git=working_copy,
    )
    records = SqliteValidatedWorkStore(
        root / "validated_work.sqlite",
        ancestry=GitValidatedWorkAncestry(
            repository=config.repo_root,
            repo_slug=config.repo,
            git=working_copy,
        ),
        artifacts=escrow,
        liveness=liveness,
        retention=escrow,
    )
    execution = LocalValidatedWorkExecutionOwner(records)
    effects = FencedValidatedWorkEffects(execution=execution, fence=records)
    ranked = RankedEvidenceAdmission(records, intake)
    blocks = AggregateRecoveryBlocks(
        repo_slug=config.repo,
        records=records,
        admission=ranked,
        phases=records,
        authority=effects,
        gate=external.gate,
        labels=label_manager,
        reader=fresh_issue_reader,
        applier=action_applier,
        human_block=human_block,
    )
    custody = ValidatedWorkCustody(escrow, blocks)
    repair = EscrowReconciliation(escrow=escrow, store=blocks)
    workspaces = EscrowPublicationWorkspaces(
        root=escrow.root,
        repository=config.repo_root,
        repo_slug=config.repo,
        escrow=escrow,
        git=git,
    )
    return ValidatedWorkRecoveryOwners(
        store=blocks,
        custody=custody,
        repair=repair,
        records=records,
        intake=intake,
        escrow=escrow,
        gate=external.gate,
        execution=execution,
        effects=effects,
        blocks=blocks,
        workspaces=workspaces,
        remote=external.remote,
        capture_observer=external.capture_observer,
        issues=external.issues,
    )


def build_validated_work_recovery(
    config: Config,
    *,
    owners: ValidatedWorkRecoveryOwners,
    completion_processor: "CompletionProcessor",
    runtime: "CoreIssueRuntimeOwners",
    working_copy: "GitWorkingCopy",
    fresh_issue_reader: "FreshIssueReader",
    action_applier: "ActionApplier",
    label_manager: "LabelManager",
) -> "RecoveryDrain":
    """Close the exact-head publication graph over the live process owners."""
    from ..control.claimed_recovery_preparation import ClaimedRecoveryPreparation
    from ..control.fenced_validated_head_publisher import FencedValidatedHeadPublisher
    from ..control.recovery_drain import RecoveryDrain
    from ..control.recovery_publication_attempt import RecoveryPublicationAttempt
    from ..control.recovery_publication_cleanup import RecoveryPublicationCleanup
    from ..control.recovery_publication_completion import RecoveryPublicationCompletion
    from ..control.recovery_record_operation import RecoveryRecordOperation
    from ..control.remote_authority_refresh import RemoteAuthorityRefreshOperation
    from ..control.retained_completion_preparation import RetainedCompletionPreparation
    from ..control.retry_review_routing import RetryReviewPolicy
    from ..control.review_exchange_lifecycle import OtherRuntimeActivity
    from ..control.staged_published_work_finalizer import StagedPublishedWorkFinalizer
    from ..execution.git_validated_head_executor import GitValidatedHeadExecutor
    from ..execution.publication_verifier import RemotePublicationVerifier

    if config.repo is None:
        raise ValueError("validated work requires configured repository identity")
    remote = owners.remote
    verifier = RemotePublicationVerifier(remote)
    publisher = FencedValidatedHeadPublisher(
        GitValidatedHeadExecutor(working_copy, remote), owners.effects
    )
    preparation = ClaimedRecoveryPreparation(
        repo_slug=config.repo,
        store=owners.records,
        effects=owners.effects,
        issues=owners.issues,
        runtime=OtherRuntimeActivity(runtime),
        gate=owners.gate,
        workspaces=owners.workspaces,
        preparation=RetainedCompletionPreparation(
            intake=owners.intake,
            completion=completion_processor,
            working_copy=working_copy,
            repo_slug=config.repo,
        ),
        pause_label=label_manager.needs_reconcile,
    )
    finalizer = StagedPublishedWorkFinalizer(
        effects=owners.effects,
        phases=owners.records,
        recovery=owners.blocks,
        fresh_issue_reader=fresh_issue_reader,
        action_applier=action_applier,
        review_policy=RetryReviewPolicy(
            code_review_agent_configured=bool(config.code_review_agent)
        ),
        routing_label=label_manager.pr_pending,
    )
    cleanup = RecoveryPublicationCleanup(
        store=owners.records,
        effects=owners.effects,
        blocks=owners.blocks,
        workspaces=owners.workspaces,
        gate=owners.gate,
    )
    completion = RecoveryPublicationCompletion(
        store=owners.records,
        effects=owners.effects,
        finalizer=finalizer,
        verifier=verifier,
        cleanup=cleanup,
        recovery_label=label_manager.recovery_pending,
    )
    operation = RecoveryRecordOperation(
        execution=owners.execution,
        store=owners.records,
        preparation=preparation,
        publication=RecoveryPublicationAttempt(
            store=owners.records,
            effects=owners.effects,
            publisher=publisher,
            verifier=verifier,
        ),
        completion=completion,
    )
    return RecoveryDrain(
        queue=owners.records,
        operation=operation,
        authority_refresh=RemoteAuthorityRefreshOperation(
            execution=owners.execution,
            effects=owners.effects,
            store=owners.records,
            observer=owners.capture_observer,
        ),
        batch_size=config.validated_work.drain_batch_size,
        interval_seconds=config.validated_work.drain_interval_seconds,
    )
