"""Shared timing policy for graceful Repository Engine shutdown."""

DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS = 120
_FORCE_SIGNAL_WAIT_SECONDS = 3.0


def signal_exit_poll_iterations(
    *, force: bool, grace_seconds: float
) -> int:
    """Return 100ms poll iterations before supervisor escalation."""
    wait_seconds = {
        True: _FORCE_SIGNAL_WAIT_SECONDS,
        False: grace_seconds,
    }[force]
    return max(1, int(wait_seconds * 10))
