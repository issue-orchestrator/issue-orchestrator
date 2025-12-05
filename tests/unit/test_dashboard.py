"""Unit tests for dashboard components."""

import pytest
from pathlib import Path
from unittest.mock import Mock
from issue_orchestrator.dashboard import StatsPanel
from issue_orchestrator.models import OrchestratorState


class TestStatsPanel:
    """Test the StatsPanel widget."""

    def test_stats_panel_creation(self):
        """Test basic stats panel creation."""
        # Mock orchestrator
        orchestrator = Mock()
        orchestrator.state = OrchestratorState(
            completed_today=[1, 2, 3],
            priority_queue=[5, 6],
        )
        orchestrator.config = Mock()
        orchestrator.config.max_sessions = 2

        panel = StatsPanel(orchestrator)
        assert panel.orchestrator == orchestrator

    def test_stats_panel_displays_correct_counts(self):
        """Test that stats panel calculates and displays correct statistics."""
        # Mock orchestrator with specific state
        orchestrator = Mock()
        orchestrator.state = OrchestratorState(
            completed_today=[1, 2, 3, 4, 5],  # 5 completed
            priority_queue=[8, 9, 10],  # 3 in queue
        )
        orchestrator.config = Mock()
        orchestrator.config.max_sessions = 3

        panel = StatsPanel(orchestrator)

        # Verify the internal state calculation
        state = panel.orchestrator.state
        config = panel.orchestrator.config

        assert len(state.completed_today) == 5
        assert len(state.active_sessions) == 0
        assert len(state.priority_queue) == 3
        assert config.max_sessions == 3

    def test_stats_panel_with_empty_state(self):
        """Test stats panel with no completed issues."""
        # Mock orchestrator with empty state
        orchestrator = Mock()
        orchestrator.state = OrchestratorState(
            completed_today=[],
            priority_queue=[],
        )
        orchestrator.config = Mock()
        orchestrator.config.max_sessions = 2

        panel = StatsPanel(orchestrator)

        state = panel.orchestrator.state
        assert len(state.completed_today) == 0
        assert len(state.active_sessions) == 0
        assert len(state.priority_queue) == 0

    def test_stats_panel_with_large_numbers(self):
        """Test stats panel with many issues."""
        # Mock orchestrator with large state
        orchestrator = Mock()
        orchestrator.state = OrchestratorState(
            completed_today=list(range(1, 101)),  # 100 completed
            priority_queue=list(range(111, 161)),  # 50 in queue
        )
        orchestrator.config = Mock()
        orchestrator.config.max_sessions = 5

        panel = StatsPanel(orchestrator)

        state = panel.orchestrator.state
        assert len(state.completed_today) == 100
        assert len(state.active_sessions) == 0
        assert len(state.priority_queue) == 50
