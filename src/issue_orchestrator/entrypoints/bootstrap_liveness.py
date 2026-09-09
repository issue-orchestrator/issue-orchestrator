"""Repository-ownership capabilities used by production composition."""

from ..infra.config import Config
from ..ports.validated_work_verification import OrchestratorLivenessPort


def held_repo_validated_work_liveness(
    config: Config, *, instance_id: str | None = None
) -> OrchestratorLivenessPort:
    """Mint recovery liveness only from this process's held startup gate."""
    from ..execution.command_runner import LocalCommandRunner
    from ..execution.repo_lock_liveness import RepoLockLiveness
    from ..infra.repo_lock import held_startup_gate

    return RepoLockLiveness(
        held_startup_gate(config.repo_root, LocalCommandRunner(), instance_id)
    )
