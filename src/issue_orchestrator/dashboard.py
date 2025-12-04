"""Dashboard UI using Rich library."""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


def create_status_bar(orchestrator: "Orchestrator") -> Text:
    """Create the status bar text."""
    state = orchestrator.state
    config = orchestrator.config

    status = "PAUSED" if state.paused else "RUNNING"
    status_color = "yellow" if state.paused else "green"

    text = Text()
    text.append("issue-orchestrator", style="bold")
    text.append(" │ Status: ")
    text.append(status, style=status_color)
    text.append(f" │ Active: {len(state.active_sessions)}/{config.max_sessions}")
    text.append(f" │ Completed: {len(state.completed_today)}")

    return text


def create_sessions_table(orchestrator: "Orchestrator") -> Table:
    """Create table of active sessions."""
    table = Table(title="Active Sessions", expand=True)

    table.add_column("#", style="cyan", width=6)
    table.add_column("Issue", style="white")
    table.add_column("Status", style="green", width=10)
    table.add_column("Runtime", style="yellow", width=8)
    table.add_column("Attach", style="dim", width=8)

    for i, session in enumerate(orchestrator.state.active_sessions, 1):
        runtime = f"{session.runtime_minutes}m"
        status = "running" if session.runtime_minutes < session.agent_config.timeout_minutes else "slow"
        status_style = "green" if status == "running" else "yellow"

        table.add_row(
            str(session.issue.number),
            session.issue.title[:40] + ("..." if len(session.issue.title) > 40 else ""),
            Text(status, style=status_style),
            runtime,
            f"[{i}]",
        )

    if not orchestrator.state.active_sessions:
        table.add_row("-", "No active sessions", "-", "-", "-")

    return table


def create_queue_table(orchestrator: "Orchestrator") -> Table:
    """Create table of queued issues."""
    table = Table(title="Queue", expand=True)

    table.add_column("#", style="cyan", width=6)
    table.add_column("Issue", style="white")
    table.add_column("Priority", style="magenta", width=10)
    table.add_column("Agent", style="blue", width=12)

    # Get queued issues (not in active sessions)
    active_numbers = {s.issue.number for s in orchestrator.state.active_sessions}

    # Show priority queue first, then would need to fetch available issues
    for issue_num in orchestrator.state.priority_queue[:5]:
        if issue_num not in active_numbers:
            table.add_row(
                str(issue_num),
                "(prioritized)",
                "HIGH",
                "-",
            )

    if not orchestrator.state.priority_queue:
        table.add_row("-", "Run to see queue", "-", "-")

    return table


def create_help_bar() -> Text:
    """Create the help bar text."""
    text = Text()
    text.append("[1-9]", style="bold cyan")
    text.append(" attach  ")
    text.append("[p]", style="bold cyan")
    text.append("ause  ")
    text.append("[r]", style="bold cyan")
    text.append("esume  ")
    text.append("[n]", style="bold cyan")
    text.append("ext  ")
    text.append("[q]", style="bold cyan")
    text.append("uit")

    return text


def create_dashboard(orchestrator: "Orchestrator") -> Layout:
    """Create the full dashboard layout."""
    layout = Layout()

    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )

    layout["header"].update(Panel(create_status_bar(orchestrator)))

    layout["body"].split_row(
        Layout(create_sessions_table(orchestrator), name="sessions"),
        Layout(create_queue_table(orchestrator), name="queue"),
    )

    layout["footer"].update(Panel(create_help_bar()))

    return layout


class Dashboard:
    """Interactive dashboard for the orchestrator."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        self.console = Console()
        self._running = False

    async def run(self) -> None:
        """Run the dashboard with live updates."""
        self._running = True

        with Live(
            create_dashboard(self.orchestrator),
            console=self.console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            while self._running and not self.orchestrator._shutdown_requested:
                live.update(create_dashboard(self.orchestrator))
                await asyncio.sleep(1)

    def stop(self) -> None:
        """Stop the dashboard."""
        self._running = False


async def run_with_dashboard(orchestrator: "Orchestrator") -> None:
    """Run orchestrator with dashboard UI."""
    dashboard = Dashboard(orchestrator)

    # Run both concurrently
    await asyncio.gather(
        orchestrator.run_loop(),
        dashboard.run(),
    )
