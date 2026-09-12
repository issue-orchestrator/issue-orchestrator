"""The configured repository slug, as a required value.

`Config.repo` is `Optional[str]` only because it can be auto-detected at load
time. Once an orchestrator is running it is REQUIRED: it becomes the scope half
of every durable work identity, travelling `Issue.key` ->
`GitHubIssueKey.scope()` -> `issue_runs.issue_scope` ->
`ValidatedWorkKey.repo_slug`.

This lives beside `Config` rather than on it: `config.py` is a tracked
`oversized_control_hotspots` file, and the point of that ratchet is that new
policy grows a focused module instead of the god object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


def require_repo(config: "Config") -> str:
    """Return `owner/name`, or raise.

    Replaces the `config.repo or ""` spelling. That fallback let an unset repo
    travel as an empty string all the way to teardown, where the only validator
    lived, so the failure surfaced hours later as a session that could never
    terminalize (#7255).
    """
    if not config.repo or not config.repo.strip():
        raise ValueError(
            "config.repo is required for work identity but is not set; "
            "set `repo: owner/name` in the orchestrator config"
        )
    # Stripped: validating the stripped form but returning the padded one would
    # let "  owner/repo " become `issue_scope` and `ValidatedWorkKey.repo_slug`.
    return config.repo.strip()
