"""Retry decorators for tmux operations using tenacity.

Simple, focused retry logic:
- Wrap libtmux calls that can fail transiently
- Exponential backoff with jitter
- Don't retry fatal errors (bad session names, tmux not installed)
"""

import logging

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

logger = logging.getLogger(__name__)

# Fatal exceptions - don't retry these
FATAL_EXCEPTIONS = (TmuxCommandNotFound, BadSessionName, OptionError, VersionTooLow)

# Session operations: 3 attempts, 0.5-4s backoff
tmux_retry = retry(
    retry=retry_if_not_exception_type(FATAL_EXCEPTIONS),
    wait=wait_exponential_jitter(initial=0.5, max=4, jitter=0.5),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
