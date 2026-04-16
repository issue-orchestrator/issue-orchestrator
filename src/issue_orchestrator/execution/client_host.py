"""Client-host integration for local UI actions.

This module owns platform-specific behavior for "open this path" style actions
requested by the dashboard. The web API exposes capability facts and delegates
to this abstraction instead of embedding platform checks in route handlers.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol


@dataclass(frozen=True)
class ClientHostCapabilities:
    """Capabilities the current UI host can provide for local path actions."""

    open_path: bool
    reveal_worktree: bool
    local_only: bool = True

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class ClientHostActionResult:
    """Result for a client-host action request."""

    path: str
    action: Literal["opened", "copy_path"]
    message: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


class ClientHost(Protocol):
    """Behavior-level API for UI host integrations."""

    def capabilities(self) -> ClientHostCapabilities:
        """Return host capabilities for local path actions."""
        ...

    def open_path(self, path: Path) -> ClientHostActionResult:
        """Open a file path in the local host if supported."""
        ...

    def reveal_worktree(self, path: Path) -> ClientHostActionResult:
        """Reveal a worktree path in the local host if supported."""
        ...


class DarwinClientHost:
    """macOS host integration backed by the ``open`` command."""

    def capabilities(self) -> ClientHostCapabilities:
        return ClientHostCapabilities(open_path=True, reveal_worktree=True)

    def open_path(self, path: Path) -> ClientHostActionResult:
        try:
            subprocess.run(["open", str(path)], check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Failed to open path: {path}") from exc
        return ClientHostActionResult(
            path=str(path),
            action="opened",
        )

    def reveal_worktree(self, path: Path) -> ClientHostActionResult:
        try:
            subprocess.run(["open", str(path)], check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Failed to open worktree: {path}") from exc
        return ClientHostActionResult(
            path=str(path),
            action="opened",
        )


class UnsupportedClientHost:
    """Fallback host when server-side local path opening is unavailable."""

    _MESSAGE = "This host cannot open server-local paths directly. Copy the path and open it in your client."

    def capabilities(self) -> ClientHostCapabilities:
        return ClientHostCapabilities(open_path=False, reveal_worktree=False)

    def open_path(self, path: Path) -> ClientHostActionResult:
        return ClientHostActionResult(
            path=str(path),
            action="copy_path",
            message=self._MESSAGE,
        )

    def reveal_worktree(self, path: Path) -> ClientHostActionResult:
        return ClientHostActionResult(
            path=str(path),
            action="copy_path",
            message=self._MESSAGE,
        )


def detect_client_host() -> ClientHost:
    """Return the platform-specific client-host implementation."""
    if platform.system() == "Darwin":
        return DarwinClientHost()
    return UnsupportedClientHost()
