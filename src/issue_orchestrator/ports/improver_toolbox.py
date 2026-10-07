"""Ports of the empowered improver's read-only toolbox (#8001).

* :class:`AuditedRepoReads` — GitHub ``GET`` of a request the toolbox policy
  (:class:`~..domain.improver_toolbox_policy.AuditedRepoReadPolicy`) already
  allowed. The adapter holds the credential; the agent never does.
* :class:`ToolboxEndpoint` — where a running toolbox serves the agent: an MCP
  server URL and the name of the environment variable that carries its
  per-run bearer token (never the token itself, never on a command line).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.improver_toolbox_policy import GitHubRead


class AuditedRepoReads(Protocol):
    def get(self, read: GitHubRead) -> Any:
        """The decoded JSON of one allowed ``GET``; raises on any failure."""
        ...


#: The MCP server name the agent sees its tools under (``mcp__<name>__<tool>``).
TOOLBOX_SERVER_NAME = "improver_toolbox"
#: The environment variable that carries the toolbox's bearer token to the agent CLI.
TOOLBOX_TOKEN_ENV = "IO_IMPROVER_TOOLBOX_TOKEN"


@dataclass(frozen=True)
class ToolboxEndpoint:
    url: str
    #: The per-run bearer token, passed to the agent CLI in its environment
    #: as :data:`TOOLBOX_TOKEN_ENV`; it grants only this run's reads.
    token: str

    def __repr__(self) -> str:  # never print the token
        return f"ToolboxEndpoint(url={self.url!r})"


__all__ = ["TOOLBOX_SERVER_NAME", "TOOLBOX_TOKEN_ENV", "AuditedRepoReads", "ToolboxEndpoint"]
