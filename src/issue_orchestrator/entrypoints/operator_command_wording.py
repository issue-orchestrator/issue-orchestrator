"""How an operator command's typed outcome is put into words (#6999 F6).

Transport policy, shared by every route that runs the operator commands: the
per-issue Retry/Dismiss buttons and the bulk unblock. One phrasing per outcome,
so an issue the command left blocked reads the same wherever it was pressed.
"""

from __future__ import annotations

from ..ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandOutcome,
    OperatorCommandStatus,
)

#: (what happened when it committed, what was attempted when it did not)
OPERATOR_COMMAND_WORDING = {
    OperatorCommandIntent.RETRY: ("queued for retry", "retried"),
    OperatorCommandIntent.DISMISS: ("dismissed", "dismissed"),
}


def unsettled_operator_command_error(outcome: OperatorCommandOutcome) -> str:
    """Why a command did not commit, in the words the operator acts on."""
    attempted = OPERATOR_COMMAND_WORDING[outcome.intent][1]
    if outcome.status is OperatorCommandStatus.STILL_BLOCKED:
        cause = (
            f"{', '.join(outcome.held_by)} still requires it"
            if outcome.held_by
            else "it could not be cleared"
        )
        return (
            f"Issue #{outcome.issue_number} was not {attempted}: "
            f"{outcome.blocked} is still on the issue because {cause}."
        )
    if outcome.status is OperatorCommandStatus.INCOMPLETE:
        return (
            f"Issue #{outcome.issue_number} was not {attempted}: failed to "
            f"remove {list(outcome.failed)} from GitHub. Removed "
            f"{list(outcome.removed)} successfully; retry the action."
        )
    raise ValueError(f"operator command outcome {outcome.status} is settled")
