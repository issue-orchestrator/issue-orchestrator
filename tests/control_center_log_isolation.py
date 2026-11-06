"""Keep the suite out of the operator's real Control Center log.

``control_center.main()`` attaches a daily-rotating file handler for
``~/.issue-orchestrator/logs/control-center.log`` to the root logger, as it
should in production. Unit tests drive ``main()`` in-process, and nothing took
that handler off again, so the rest of that xdist worker's tests logged into the
log of the Control Center actually running on the host. On 2026-11-05 about 94%
of that file was pytest output, including made-up supervisor records
(``Orchestrator started with PID 99999``) and ``[ERROR]`` lines describing test
fixtures. Those supervisor records are the reason the file exists. Every test
now runs inside :func:`control_center_log_isolated` (an autouse fixture in
``tests/conftest.py``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def control_center_log_isolated(log_file: Path) -> Iterator[None]:
    """Send the Control Center's file log to ``log_file`` and detach it on exit.

    Only the handler for ``log_file`` is removed. A test's other handlers stay
    that test's business.
    """
    from issue_orchestrator.infra import logging_config

    real_log_path = logging_config.get_control_center_log_path
    logging_config.get_control_center_log_path = lambda: log_file
    try:
        yield
    finally:
        logging_config.get_control_center_log_path = real_log_path
        root = logging.getLogger()
        target = os.path.abspath(log_file)
        for handler in list(root.handlers):
            if (
                isinstance(handler, logging.FileHandler)
                and handler.baseFilename == target
            ):
                root.removeHandler(handler)
                handler.close()
