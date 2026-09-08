"""Report inert, damaged and partial captures. Doctor never repairs or deletes."""

from ....control.validated_work_escrow import EscrowInspection
from ....ports.command_runner import CommandRunner
from ...config import Config
from ..types import Check


def check_validated_work(config: Config, runner: CommandRunner | None) -> list[Check]:
    from ....adapters.git.git_cli import GitCLI
    from ....execution.git_working_copy import GitWorkingCopy
    from ...validated_work_escrow import FilesystemValidatedWorkEscrow
    from ...validated_work_inspection import SqliteValidatedWorkEvidenceReader

    if runner is None:
        return [
            Check(
                "Validated work escrow",
                "info",
                "Git inspection unavailable; escrow retained",
            )
        ]
    try:
        if config.repo is None:
            raise ValueError("repository identity is not configured")
        root = config.repo_root / ".issue-orchestrator" / "state"
        escrow = FilesystemValidatedWorkEscrow(
            root / "validated-work",
            repository=config.repo_root,
            repo_slug=config.repo,
            git=GitWorkingCopy(git=GitCLI(runner=runner)),
        )
        report = EscrowInspection(
            escrow=escrow,
            reader=SqliteValidatedWorkEvidenceReader(root / "validated_work.sqlite"),
        ).inspect_orphans()
        return [
            Check("Validated work escrow", "warning", f"{p.locator}: {p.detail}")
            for p in report.problems
        ] or [Check("Validated work escrow", "ok", "No orphaned or damaged captures")]
    except Exception as exc:
        return [
            Check(
                "Validated work escrow",
                "warning",
                f"Inspection failed; escrow retained: {exc}",
            )
        ]
