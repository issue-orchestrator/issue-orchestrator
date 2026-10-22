"""The one terminal boundary every agent launch goes through (#9012).

A launch composes its :class:`LaunchCommand`, asks the terminal to prepare it,
then — after its workflow mutations — starts the session from what the terminal
prepared. Both halves live here so no launch path can start a session from a
command its terminal never proved it can exec, nor without its provider
credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.launch_prompt_delivery import LaunchCommand, PreparedSessionCommand
from ..ports.provider_credentials import ProviderCredentials

if TYPE_CHECKING:
    from ..domain.models import AgentConfig
    from ..ports.session_launcher_factory import CreateSessionFn


class PrepareSessionFn(Protocol):
    """The terminal's preflight: its exact exec form of a command, or ``LaunchCommandTooLong``."""

    def __call__(self, command: LaunchCommand, working_dir: Path, /) -> PreparedSessionCommand: ...


@dataclass(frozen=True, slots=True)
class LaunchTerminal:
    """Prepare a launch's command, then start its session from that same value."""

    prepare_session: PrepareSessionFn
    create_session: "CreateSessionFn"
    credentials: ProviderCredentials

    def prepare(self, command: LaunchCommand, worktree: Path) -> PreparedSessionCommand:
        """The command exactly as the terminal will exec it in *worktree*.

        Side-effect free and raises
        :class:`~..domain.launch_prompt_delivery.LaunchCommandTooLong` for a
        command the OS would refuse, so a launch calls it before its first
        workflow mutation: such a launch fails with nothing to compensate.
        """
        return self.prepare_session(command, worktree)

    def start(
        self, name: str, prepared: PreparedSessionCommand, title: str, agent_config: "AgentConfig",
    ) -> bool:
        """Start one agent session from *prepared*, always with its provider credentials.

        The single place an agent session is started. Credentials are resolved
        here rather than at each launch path so a new path cannot launch a
        key-authenticated provider unauthenticated — the failure looks like a
        bad key rather than a missing wire-up. ``None`` rather than an empty
        mapping when there are none, so "this provider needs no key" and "the
        key resolved to nothing" stay distinguishable to the terminal.
        """
        secret_env = dict(self.credentials.session_env(agent_config.provider))
        return self.create_session(name, prepared, title, secret_env or None)
