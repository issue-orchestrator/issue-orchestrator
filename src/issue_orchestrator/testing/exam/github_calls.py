"""GitHub calls per endpoint class, read from the orchestrator's gh_audit report.

GitHub budgets its API per resource: search (30/min), GraphQL (points/hour)
and core REST (5000/hour) run out independently, and #7298 was exactly a
search-budget exhaustion hidden inside a healthy-looking core total. So the
exam reports calls per class, not one number.

The audit report keys every REST call as ``"<METHOD> <path>"`` (no query
string) and GraphQL as ``"POST /graphql"``; anything else it records (a ``gh``
subcommand from an older build, say) is reported as ``other`` rather than
guessed into a class.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})


class EndpointClass(str, Enum):
    """GitHub rate-limit resource a call is charged to."""

    SEARCH = "search"
    GRAPHQL = "graphql"
    CORE = "core"
    OTHER = "other"


def classify_command(command: str) -> EndpointClass:
    """Class of one gh_audit ``by_command`` key."""
    method, _, path = command.strip().partition(" ")
    if method.upper() not in _HTTP_METHODS or not path.startswith("/"):
        return EndpointClass.OTHER
    if path == "/graphql" or path.startswith("/graphql/"):
        return EndpointClass.GRAPHQL
    if path.startswith("/search/"):
        return EndpointClass.SEARCH
    return EndpointClass.CORE


@dataclass(frozen=True)
class GitHubCallCounts:
    """Calls the system made during the run, per endpoint class."""

    by_class: Mapping[EndpointClass, int]
    top_commands: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        return sum(self.by_class.values())

    def count(self, endpoint_class: EndpointClass) -> int:
        return self.by_class.get(endpoint_class, 0)

    @classmethod
    def between(
        cls,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any],
        *,
        top: int = 8,
    ) -> "GitHubCallCounts":
        """Calls made between two audit reports of the SAME process.

        ``before`` is ``None`` when the run's first report is the baseline
        (a fresh process), so every call in ``after`` counts.
        """
        before_counts = _by_command(before) if before is not None else {}
        delta: dict[str, int] = {}
        for command, count in _by_command(after).items():
            spent = count - before_counts.get(command, 0)
            if spent < 0:
                raise ValueError(
                    f"gh_audit count for {command!r} went backwards"
                    f" ({before_counts[command]} -> {count}); the two reports"
                    " are not from the same process"
                )
            if spent:
                delta[command] = spent
        by_class = {endpoint_class: 0 for endpoint_class in EndpointClass}
        for command, spent in delta.items():
            by_class[classify_command(command)] += spent
        ranked = sorted(delta.items(), key=lambda item: (-item[1], item[0]))
        return cls(by_class=by_class, top_commands=tuple(ranked[:top]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_class": {
                endpoint_class.value: self.count(endpoint_class)
                for endpoint_class in EndpointClass
            },
            "top_commands": [
                {"command": command, "calls": calls}
                for command, calls in self.top_commands
            ],
        }


    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GitHubCallCounts":
        """Inverse of :meth:`to_dict` (``total`` is derived, not read)."""
        by_class = data["by_class"]
        return cls(
            by_class={c: int(by_class[c.value]) for c in EndpointClass},
            top_commands=tuple(
                (str(entry["command"]), int(entry["calls"])) for entry in data["top_commands"]
            ),
        )


def _by_command(report: Mapping[str, Any]) -> dict[str, int]:
    raw = report.get("by_command")
    if not isinstance(raw, Mapping):
        raise ValueError("gh_audit report has no by_command mapping")
    counts: dict[str, int] = {}
    for command, count in raw.items():
        if not isinstance(command, str) or isinstance(count, bool) or not isinstance(count, int):
            raise ValueError(f"malformed gh_audit by_command entry: {command!r}={count!r}")
        counts[command] = count
    return counts
