"""One fail-closed normalization contract for pre-authority captures."""

from ..domain.validated_work import ValidatedWorkFailure, ValidatedWorkState


LEGACY_REMOTE_AUTHORITY_REASON = (
    "Legacy capture lacked an authoritative remote branch/PR observation; "
    "preserved pending recovery approval"
)


def normalize_legacy_remote_authority(
    state: ValidatedWorkState,
    failure: ValidatedWorkFailure | None,
    reason: str,
) -> tuple[ValidatedWorkState, ValidatedWorkFailure | None, str]:
    """Park legacy automatic work while retaining other captured dispositions."""
    if state is not ValidatedWorkState.QUEUED:
        return state, failure, reason
    return (
        ValidatedWorkState.PARKED,
        ValidatedWorkFailure.REMOTE_UNREADABLE,
        LEGACY_REMOTE_AUTHORITY_REASON,
    )
