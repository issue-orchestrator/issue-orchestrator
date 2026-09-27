"""The one owner of "a dependency lookup failed, so the edge is UNKNOWN" (#7297).

Both of the dependency evaluator's host reads - the predecessor snapshot and
the external-ID resolution - turn a failed lookup into an UNKNOWN edge.
Building that edge in one place is what keeps the two paths agreeing on what
the failure carries. In particular, both keep the host rate limit that caused
it: planning still reads the edge as UNKNOWN, while a launch defers on the
reset instead of failing the work.
"""

from __future__ import annotations

from ..domain.dependencies import Dependency, DependencyMode, DependencyState
from ..ports.repository_host import host_rate_limit_of


def unknown_dependency(
    *,
    cause: Exception,
    error: str,
    issue_number: int | None,
    external_id: str | None,
    repository: str | None = None,
    mode: DependencyMode = DependencyMode.NORMAL,
) -> Dependency:
    """An UNKNOWN edge whose lookup raised ``cause``."""
    return Dependency(
        issue_number=issue_number,
        external_id=external_id,
        repository=repository,
        mode=mode,
        state=DependencyState.UNKNOWN,
        error=error,
        host_rate_limit=host_rate_limit_of(cause),
    )


__all__ = ["unknown_dependency"]
