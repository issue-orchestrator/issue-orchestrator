"""The autouse isolation of the web orchestrator slots (tests/web_globals.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

from issue_orchestrator.entrypoints import control_api, web
from tests.web_globals import web_globals_isolated


def test_a_stub_installed_inside_the_scope_is_gone_after_it() -> None:
    before = (web.get_orchestrator(), control_api.get_orchestrator())
    stub = MagicMock(name="leaky stub orchestrator")

    with web_globals_isolated():
        web.set_orchestrator(stub)
        control_api.set_orchestrator(stub)

    assert (web.get_orchestrator(), control_api.get_orchestrator()) == before


def test_every_test_starts_without_a_leaked_orchestrator() -> None:
    """The autouse fixture wraps this test too: nothing an earlier test on this
    worker installed and never restored is visible here."""
    assert web.get_orchestrator() is None
    assert control_api.get_orchestrator() is None
