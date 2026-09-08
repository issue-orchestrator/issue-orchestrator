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
