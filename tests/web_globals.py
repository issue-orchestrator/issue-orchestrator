"""Isolation of the web entrypoints' process-wide orchestrator slots.

``entrypoints.web`` and ``entrypoints.control_api`` keep the running
orchestrator (and the web server) in module globals that ``set_orchestrator``
writes. Many tests install a stub there and never put the previous value back,
so under xdist whatever a worker ran earlier leaked into a later test's
dashboard render: ``test_authenticated_root_renders_csrf_meta_and_browser_auth_script``
failed on a ``SimpleNamespace`` session left by a reset-route test. Every test
now runs inside :func:`web_globals_isolated` (an autouse fixture in
``tests/conftest.py``), which restores the slots it found.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

#: (module, attribute) of every process-wide slot a web test may write.
_SLOTS = (
    ("issue_orchestrator.entrypoints.web", "_orchestrator"),
    ("issue_orchestrator.entrypoints.web", "_server"),
    ("issue_orchestrator.entrypoints.control_api", "_orchestrator"),
)


@contextmanager
def web_globals_isolated() -> Iterator[None]:
    """Restore every web orchestrator/server slot to what it was on entry.

    Modules the test has not imported are not imported here; a slot of a module
    first imported during the test is reset to ``None``, its initial value.
    """
    saved = {
        slot: getattr(sys.modules[slot[0]], slot[1])
        for slot in _SLOTS
        if slot[0] in sys.modules
    }
    try:
        yield
    finally:
        for module_name, attribute in _SLOTS:
            module = sys.modules.get(module_name)
            if module is not None:
                setattr(module, attribute, saved.get((module_name, attribute)))
