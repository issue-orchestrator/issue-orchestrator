"""Dashboard UI using Textual library with full keyboard support."""

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Awaitable

from textual.app import App, ComposeResult

logger = logging.getLogger(__name__)
from textual.widgets import Static, DataTable, Footer, Header
from textual.containers import Container, Horizontal, Vertical
from textual.binding import Binding
from textual.reactive import reactive
from rich.text import Text

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class StatusBar(Static):
    """Status bar showing orchestrator state."""

    def __init__(self, orchestrator: "Orchestrator", **kwargs) -> None:
        super().__init__(**kwargs)
        self.orchestrator = orchestrator
        logger.debug("StatusBar.__init__ called")

    def on_mount(self) -> None:
        """Set initial content when widget is mounted."""
        logger.debug("StatusBar.on_mount called")
        content = self._render_content()
        logger.debug("StatusBar content: %s", content)
        self.update(content)

    def _render_content(self) -> str:
        """Render the status bar content."""
        state = self.orchestrator.state
        config = self.orchestrator.config

        status = "[yellow]PAUSED[/yellow]" if state.paused else "[green]RUNNING[/green]"

        return f"[bold]issue-orchestrator[/bold] │ Status: {status} │ Active: {len(state.active_sessions)}/{config.max_sessions} │ Completed: {len(state.completed_today)}"

    def refresh_content(self) -> None:
        """Update the status bar with current state."""
        self.update(self._render_content())


class SessionsTable(Static):
    """Table showing active sessions."""

    def __init__(self, orchestrator: "Orchestrator", **kwargs) -> None:
        super().__init__(**kwargs)
        self.orchestrator = orchestrator

    def compose(self) -> ComposeResult:
        table = DataTable()
        table.add_column("#", width=6)
        table.add_column("Issue", width=40)
        table.add_column("Status", width=10)
        table.add_column("Runtime", width=8)
        table.add_column("Key", width=5)
        yield table

    def update_table(self) -> None:
        """Update the table with current session data."""
        table = self.query_one(DataTable)
        table.clear()

        sessions = self.orchestrator.state.active_sessions
        if not sessions:
            table.add_row("-", "No active sessions", "-", "-", "-")
            return

        for i, session in enumerate(sessions, 1):
            runtime = f"{session.runtime_minutes}m"
            status = "running" if session.runtime_minutes < session.agent_config.timeout_minutes else "slow"
            status_style = "green" if status == "running" else "yellow"
            title = session.issue.title[:37] + "..." if len(session.issue.title) > 40 else session.issue.title

            table.add_row(
                str(session.issue.number),
                title,
                Text(status, style=status_style),
                runtime,
                f"[{i}]",
            )


class QueueTable(Static):
    """Table showing queued issues."""

    def __init__(self, orchestrator: "Orchestrator", **kwargs) -> None:
        super().__init__(**kwargs)
        self.orchestrator = orchestrator

    def compose(self) -> ComposeResult:
        table = DataTable()
        table.add_column("#", width=6)
        table.add_column("Issue", width=30)
        table.add_column("Priority", width=10)
        yield table

    def update_table(self) -> None:
        """Update the queue table."""
        table = self.query_one(DataTable)
        table.clear()

        active_numbers = {s.issue.number for s in self.orchestrator.state.active_sessions}
        queue = [n for n in self.orchestrator.state.priority_queue if n not in active_numbers]

        if not queue:
            table.add_row("-", "Queue empty", "-")
            return

        for issue_num in queue[:10]:
            table.add_row(str(issue_num), "(prioritized)", "HIGH")


class DashboardApp(App):
    """Textual app for the orchestrator dashboard."""

    TITLE = "issue-orchestrator"
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2;
        grid-columns: 2fr 1fr;
        grid-rows: auto 1fr auto;
    }

    #status-bar {
        column-span: 2;
        height: auto;
        min-height: 3;
        padding: 0 1;
        background: $surface;
        border: solid $primary;
    }

    #sessions {
        height: 100%;
        padding: 1;
        border: solid $primary;
    }

    #queue {
        height: 100%;
        padding: 1;
        border: solid $secondary;
    }

    DataTable {
        height: 100%;
    }

    Footer {
        column-span: 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("n", "next", "Next Issue"),
        Binding("1", "attach(1)", "1=Attach", show=True),
        Binding("2", "attach(2)", "2", show=False),
        Binding("3", "attach(3)", "3", show=False),
        Binding("4", "attach(4)", "4", show=False),
        Binding("5", "attach(5)", "5", show=False),
        Binding("6", "attach(6)", "6", show=False),
        Binding("7", "attach(7)", "7", show=False),
        Binding("8", "attach(8)", "8", show=False),
        Binding("9", "attach(9)", "9", show=False),
    ]

    def __init__(
        self,
        orchestrator: "Orchestrator",
        on_pause: Callable[[], Awaitable[None]] | None = None,
        on_resume: Callable[[], Awaitable[None]] | None = None,
        on_next: Callable[[], Awaitable[None]] | None = None,
        on_attach: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_next = on_next
        self._on_attach = on_attach
        self._refresh_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield StatusBar(self.orchestrator, id="status-bar")
        yield SessionsTable(self.orchestrator, id="sessions")
        yield QueueTable(self.orchestrator, id="queue")
        yield Footer()

    async def on_mount(self) -> None:
        """Start the refresh loop when mounted."""
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        """Refresh the display every second."""
        while True:
            try:
                # Update all dynamic widgets
                self.query_one("#status-bar", StatusBar).refresh_content()
                self.query_one("#sessions", SessionsTable).update_table()
                self.query_one("#queue", QueueTable).update_table()

                # Check if orchestrator requested shutdown
                if self.orchestrator._shutdown_requested:
                    self.exit()
                    return

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return
            except Exception:
                # Don't crash the refresh loop on errors
                await asyncio.sleep(1)

    async def action_quit(self) -> None:
        """Handle quit action."""
        if self._refresh_task:
            self._refresh_task.cancel()
        self.exit()

    async def action_pause(self) -> None:
        """Handle pause action."""
        if self._on_pause:
            await self._on_pause()
        else:
            self.orchestrator.state.paused = True
        self.notify("Orchestrator paused")

    async def action_resume(self) -> None:
        """Handle resume action."""
        if self._on_resume:
            await self._on_resume()
        else:
            self.orchestrator.state.paused = False
        self.notify("Orchestrator resumed")

    async def action_next(self) -> None:
        """Handle next issue action."""
        if self._on_next:
            await self._on_next()
        self.notify("Next issue prioritized")

    async def action_attach(self, index: int) -> None:
        """Handle attach to session action."""
        logger.debug("action_attach called with index=%d", index)
        try:
            sessions = self.orchestrator.state.active_sessions
            logger.debug("Found %d active sessions", len(sessions))
            if index <= len(sessions):
                session = sessions[index - 1]
                logger.debug("Attaching to session for issue #%d", session.issue.number)
                if self._on_attach:
                    logger.debug("Using custom on_attach callback")
                    await self._on_attach(session.issue.number)
                else:
                    # Default: use tmux to switch to the session window
                    logger.debug("Using default tmux attach")
                    from .tmux import get_manager
                    manager = get_manager()
                    logger.debug("Got tmux manager: %s, session: %s", manager, getattr(manager, 'session', None))
                    if manager and manager.session:
                        # select_window expects issue number (int), not window name
                        logger.debug("Calling select_window(%d)", session.issue.number)
                        if manager.select_window(session.issue.number):
                            logger.debug("select_window succeeded, exiting dashboard")
                            self.exit()  # Exit dashboard to show the session
                        else:
                            logger.warning("Window for #%d not found", session.issue.number)
                            self.notify(f"Window for #{session.issue.number} not found", severity="warning")
                    else:
                        logger.error("Tmux session not available")
                        self.notify("Tmux session not available", severity="error")
            else:
                logger.warning("No session at index %d (only %d sessions)", index, len(sessions))
                self.notify(f"No session at index {index}", severity="warning")
        except Exception as e:
            logger.exception("Attach failed: %s", e)
            self.notify(f"Attach failed: {e}", severity="error")


class Dashboard:
    """Wrapper class for backward compatibility."""

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self.orchestrator = orchestrator
        self._app: DashboardApp | None = None
        self.attach_after_exit: bool = False  # Set when user presses 1-9

    async def run(self) -> None:
        """Run the dashboard."""
        self._app = DashboardApp(
            self.orchestrator,
            on_pause=self._handle_pause,
            on_resume=self._handle_resume,
            on_attach=self._handle_attach,
        )
        await self._app.run_async()

    async def _handle_attach(self, issue_number: int) -> None:
        """Handle attach - just mark that we should attach after exit."""
        from .tmux import get_manager
        manager = get_manager()
        if manager and manager.session:
            manager.select_window(issue_number)
            self.attach_after_exit = True
            if self._app:
                self._app.exit()

    async def _handle_pause(self) -> None:
        """Handle pause from dashboard."""
        self.orchestrator.state.paused = True

    async def _handle_resume(self) -> None:
        """Handle resume from dashboard."""
        self.orchestrator.state.paused = False

    def stop(self) -> None:
        """Stop the dashboard."""
        if self._app:
            self._app.exit()


async def run_with_dashboard(orchestrator: "Orchestrator") -> bool:
    """Run orchestrator with dashboard UI.

    The orchestrator runs in a background task while the dashboard
    handles the UI and keyboard input in the foreground.

    Returns True if the caller should attach to the tmux session.
    """
    dashboard = Dashboard(orchestrator)

    async def run_orchestrator():
        """Run the orchestrator loop, stopping when dashboard exits."""
        try:
            await orchestrator.run_loop()
        except asyncio.CancelledError:
            pass

    # Start orchestrator in background
    orchestrator_task = asyncio.create_task(run_orchestrator())

    try:
        # Run dashboard in foreground (handles keyboard input)
        await dashboard.run()
    finally:
        # When dashboard exits, stop orchestrator
        orchestrator._shutdown_requested = True
        orchestrator_task.cancel()
        try:
            await orchestrator_task
        except asyncio.CancelledError:
            pass

    return dashboard.attach_after_exit
