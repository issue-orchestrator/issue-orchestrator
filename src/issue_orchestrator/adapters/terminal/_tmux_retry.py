"""Retry decorators for tmux operations using tenacity.

Simple, focused retry logic:
- Wrap libtmux calls that can fail transiently
- Exponential backoff with jitter
- Don't retry fatal errors (bad session names, tmux not installed)
- Clear stale session cache on retry
"""

import logging
from typing import TYPE_CHECKING

from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)
from libtmux.exc import (
    BadSessionName,
    OptionError,
    TmuxCommandNotFound,
    VersionTooLow,
)

if TYPE_CHECKING:
    from tenacity import RetryCallState

logger = logging.getLogger(__name__)


class PaneAlreadyExistsError(ValueError):
    """Raised when trying to create a pane that already exists."""
    pass


# Fatal exceptions - don't retry these
FATAL_EXCEPTIONS = (TmuxCommandNotFound, BadSessionName, OptionError, VersionTooLow, PaneAlreadyExistsError)


def _clear_session_cache(retry_state: "RetryCallState") -> None:
    """Clear the session cache before retrying.

    When a tmux operation fails, the cached session reference may be stale
    (e.g., tmux session was killed externally). Clearing it forces a fresh
    lookup on retry.
    """
    # Get the 'self' argument (first positional arg for instance methods)
    args = retry_state.args
    if args and hasattr(args[0], "_session"):
        manager = args[0]
        if manager._session is not None:
            logger.info("[TMUX] Clearing stale session cache before retry")
            manager._session = None


def _before_sleep_with_cache_clear(retry_state: "RetryCallState") -> None:
    """Log retry and clear session cache."""
    before_sleep_log(logger, logging.WARNING)(retry_state)
    _clear_session_cache(retry_state)


# Session operations: 3 attempts, 0.5-4s backoff, clears session cache on retry
tmux_retry = retry(
    retry=retry_if_not_exception_type(FATAL_EXCEPTIONS),
    wait=wait_exponential_jitter(initial=0.5, max=4, jitter=0.5),
    stop=stop_after_attempt(3),
    before_sleep=_before_sleep_with_cache_clear,
    reraise=True,
)
