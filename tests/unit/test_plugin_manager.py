"""Unit tests for plugin manager loading."""

from issue_orchestrator.execution.manager import create_plugin_manager


def test_subprocess_plugin_can_load():
    """Ensure subprocess plugin mapping resolves to a valid class."""
    pm = create_plugin_manager(terminal_plugin="subprocess", load_entry_points=False)
    assert pm is not None
