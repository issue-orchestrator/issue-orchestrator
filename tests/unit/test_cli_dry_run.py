"""Direct tests for CLI dry-run formatting helpers."""

from issue_orchestrator.entrypoints.cli_dry_run import (
    format_orphan_action,
    format_orphan_issue_info,
)
from issue_orchestrator.infra.analysis import OrphanBranchState


def test_format_orphan_issue_info_with_open_issue_title() -> None:
    orphan = OrphanBranchState(
        issue_number=123,
        branch_name="123-feature",
        issue_state="open",
        issue_title="Implement a very important feature with a long title",
    )

    assert (
        format_orphan_issue_info(orphan)
        == "[green]open[/green]: Implement a very importan..."
    )


def test_format_orphan_issue_info_without_title() -> None:
    orphan = OrphanBranchState(
        issue_number=123,
        branch_name="123-feature",
        issue_state="closed",
    )

    assert format_orphan_issue_info(orphan) == "[red]closed[/red]"


def test_format_orphan_issue_info_when_issue_missing() -> None:
    orphan = OrphanBranchState(issue_number=123, branch_name="123-feature")

    assert format_orphan_issue_info(orphan) == "[dim]not found[/dim]"


def test_format_orphan_action_uses_known_styles() -> None:
    assert (
        format_orphan_action(
            OrphanBranchState(
                issue_number=123,
                branch_name="123-feature",
                issue_state="open",
                commits_ahead=1,
            )
        )
        == "[green]resume[/green]"
    )
    assert (
        format_orphan_action(
            OrphanBranchState(issue_number=123, branch_name="123-feature")
        )
        == "[red]delete[/red]"
    )


def test_format_orphan_action_falls_back_to_unknown_action() -> None:
    class UnknownAction(OrphanBranchState):
        @property
        def suggested_action(self) -> str:
            return "archive"

    assert (
        format_orphan_action(UnknownAction(issue_number=123, branch_name="123-feature"))
        == "archive"
    )
