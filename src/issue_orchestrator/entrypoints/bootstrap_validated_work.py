"""Composition-root factory for the slice-2 escrow maintenance capabilities."""

from ..infra.config import Config
from ..ports.exact_git import ExactGit
from ..ports.validated_work_store import ValidatedWorkStore
from ..control.validated_work_escrow import ValidatedWorkEscrowMaintenance


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


from dataclasses import dataclass
from ..control.validated_work_capture import ParkedEvidenceCustody
from ..control.validated_work_escrow import EscrowReconciliation
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore


@dataclass(frozen=True, slots=True)
class ValidatedWorkAdmissionOwners:
    store: ValidatedWorkAdmissionStore
    custody: ParkedEvidenceCustody
    repair: EscrowReconciliation


def build_validated_work_admission(config: Config, working_copy: ExactGit) -> ValidatedWorkAdmissionOwners:
    from ..infra.repo_identity import state_dir
    from ..infra.validated_work_escrow import FilesystemValidatedWorkEscrow
    from ..infra.validated_work_intake_store import SqliteValidatedWorkIntakeStore
    from ..execution.validated_work_ancestry import GitValidatedWorkAncestry

    if config.repo is None:
        raise ValueError("validated work requires configured repository identity")
    root = state_dir(config.repo_root)
    escrow = FilesystemValidatedWorkEscrow(root / "validated-work", repository=config.repo_root, repo_slug=config.repo, git=working_copy)
    ancestry = GitValidatedWorkAncestry(repository=config.repo_root, repo_slug=config.repo, git=working_copy)
    store = SqliteValidatedWorkIntakeStore(root / "validated_work.sqlite", ancestry, escrow)
    return ValidatedWorkAdmissionOwners(store, ParkedEvidenceCustody(escrow, store), EscrowReconciliation(escrow=escrow, store=store))
