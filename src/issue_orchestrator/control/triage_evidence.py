"""Owner for the triage EVIDENCE MAP — a god-view location manifest + warm-cache.

The FAILURE_INVESTIGATION triage agent (ADR-0031) receives only
``board-snapshot.json`` today. That leaves it blind to the raw evidence a
good failure investigation actually reasons from: the session run-dirs, the
orchestrator log, the ``timeline.sqlite`` event store, and GitHub ground
truth. This module builds and writes an ``evidence-map.json`` next to the
board snapshot that POINTS the agent at those locations (read access it
already has) plus a best-effort GitHub warm-cache. Writes stay unchanged —
still gated / orchestrator-executed; this only stages read-side evidence.

Best-effort by design (a deliberate exception to the repo's fail-fast house
style): unlike the board snapshot, the evidence map is an ENHANCEMENT, never a
required input, so a failure to build or write it must NOT fail the session
launch. The GitHub warm-cache is doubly best-effort — any port/network error
yields a ``null`` ``github`` block rather than propagating — because the
public repo lets the agent verify everything itself with local ``git`` when
the cache is null or thin. The call-site wrapper
(``triage_session_policy._stage_evidence_map``) owns the outer catch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..infra.logging_config import get_repo_log_path
from ..infra.repo_identity import state_dir

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports import RepositoryHost

logger = logging.getLogger(__name__)

EVIDENCE_MAP_SCHEMA_VERSION = 1

# Canonical evidence-map filename inside a session's triage-data directory,
# next to BOARD_SNAPSHOT_FILENAME (domain/board_snapshot.py).
EVIDENCE_MAP_FILENAME = "evidence-map.json"

# Sub-directory under a session worktree that holds run-dirs. Mirrors
# ``execution.session_output_adapter.SESSION_OUTPUT_DIR`` ("sessions"); the
# literal is duplicated here to keep this control-layer owner off an
# execution-adapter import.
_SESSIONS_SUBDIR = "sessions"

# Default branch the agent verifies merge-reachability against. GitHub repos
# default to ``main``; a repo whose default differs sets it explicitly via
# ``worktrees.base_branch_override`` (config.worktree_base_branch_override),
# which is the same override the worktree base-branch resolver keys on. Reading
# the true default over git would need a git port this launch-prep path does
# not carry — the guidance text tells the agent to confirm with local git.
_DEFAULT_BRANCH_FALLBACK = "main"

_GUIDANCE = (
    "Read the run-dir artifacts (run-audit.json, validation-record.json, "
    "completion-record.json, analysis.json). Key on validation.passed, not the "
    "outcome string. This repo is PUBLIC: verify merge-reachability with local "
    "git (e.g. `git -C <run_dir> fetch origin --quiet && git merge-base "
    "--is-ancestor <merge_commit_oid> origin/<default_branch>`). timeline_sqlite "
    "is a read-only SQLite event store you can query with sqlite3. In github.prs, "
    "branch_matches_focus=true is THIS issue's own implementation PR; "
    "branch_matches_focus=false only references the issue (e.g. a meta/rework PR) "
    "and is NOT its implementation - do not treat it as the issue's work. The "
    "github block is a best-effort warm-cache; when it is null or thin, gather "
    "the rest yourself with git."
)


@dataclass(frozen=True)
class GithubIssueCache:
    """Warm-cache of the focus issue's ground-truth state + labels."""

    number: int
    state: str
    labels: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "labels": list(self.labels),
        }


@dataclass(frozen=True)
class GithubPrCache:
    """Warm-cache of one PR the tracker links to the focus issue.

    ``branch_matches_focus`` distinguishes the issue's OWN implementation PR
    (head branch ``<issue>-<slug>``) from a PR that merely *references* the
    issue in its title/body (e.g. a meta/rework PR). The tracker's
    ``get_prs_for_issue`` matches on both branch prefix AND title-contains-#N,
    so a ``False`` here means "mentions this issue, is not its implementation" —
    surfaced rather than dropped because it is useful "handled elsewhere?"
    signal, but flagged so it cannot be mistaken for the issue's own work.

    ``merge_commit_oid`` is always ``None``: the ``PullRequestTracker`` port's
    ``PRInfo`` does not carry the merge commit, so it is left unknown rather
    than fabricated (the guidance tells the agent to resolve merge-reachability
    with local git).
    """

    number: int
    state: str
    merged: bool
    base_ref: str | None
    head_ref: str | None
    branch_matches_focus: bool
    merge_commit_oid: str | None
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "merged": self.merged,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "branch_matches_focus": self.branch_matches_focus,
            "merge_commit_oid": self.merge_commit_oid,
            "url": self.url,
        }


@dataclass(frozen=True)
class GithubWarmCache:
    """Best-effort GitHub ground-truth read for the focus issue and its PRs."""

    issue: GithubIssueCache | None
    prs: tuple[GithubPrCache, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue.to_dict() if self.issue is not None else None,
            "prs": [pr.to_dict() for pr in self.prs],
        }


@dataclass(frozen=True)
class EvidenceMap:
    """A triage session's read-side evidence manifest (schema_version 1).

    All on-disk locations are absolute path strings. ``github`` is ``None``
    for a locations-only map (health review) or when the warm-cache read
    failed; ``focus_issue_number`` is ``None`` and ``run_dirs`` empty when
    there is no single focus issue.
    """

    focus_issue_number: int | None
    repo: str
    default_branch: str
    state_dir: str
    orchestrator_log: str
    timeline_sqlite: str
    run_dirs: tuple[str, ...]
    github: GithubWarmCache | None
    schema_version: int = EVIDENCE_MAP_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "focus_issue_number": self.focus_issue_number,
            "repo": self.repo,
            "default_branch": self.default_branch,
            "state_dir": self.state_dir,
            "orchestrator_log": self.orchestrator_log,
            "timeline_sqlite": self.timeline_sqlite,
            "run_dirs": list(self.run_dirs),
            "github": self.github.to_dict() if self.github is not None else None,
            "guidance": _GUIDANCE,
        }


def _resolve_default_branch(config: "Config") -> str:
    """The branch the agent checks merge-reachability against."""
    override = config.worktree_base_branch_override
    if override:
        override = override.strip()
        if override:
            return override
    return _DEFAULT_BRANCH_FALLBACK


def _session_worktree(config: "Config", focus_issue_number: int) -> Path:
    """The orchestrator-managed session worktree, a sibling of the main repo.

    Mirrors ``adapters.worktree._worktree`` layout:
    ``<worktree_base>/<repo_root.name>-<issue_number>`` where ``worktree_base``
    defaults to ``repo_root.parent`` (config resolves ``worktrees.base`` to an
    absolute path at load time).
    """
    repo_root = Path(config.repo_root)
    worktree_base = Path(config.worktree_base) if config.worktree_base else repo_root.parent
    return worktree_base / f"{repo_root.name}-{focus_issue_number}"


def _resolve_run_dirs(
    config: "Config",
    focus_issue_number: int | None,
    artifact_hints: Sequence[str],
) -> tuple[str, ...]:
    """Absolute, de-duped, sorted run-dir paths for the focus issue.

    Two best-effort sources are merged: (1) a glob of the focus issue's
    sibling session worktree for ``*__*`` run directories that exist, and
    (2) the parent directories of any ``artifact_hints`` carried on the focus
    failure (each hint is an absolute path to a file inside a run/log dir).
    Returns an empty tuple when there is no focus issue or the worktree is gone.
    """
    found: set[str] = set()
    if focus_issue_number is not None:
        sessions_dir = (
            _session_worktree(config, focus_issue_number)
            / ".issue-orchestrator"
            / _SESSIONS_SUBDIR
        )
        if sessions_dir.is_dir():
            for entry in sessions_dir.glob("*__*"):
                if entry.is_dir():
                    found.add(str(entry.resolve()))
    for hint in artifact_hints:
        if not hint:
            continue
        parent = Path(hint).parent
        found.add(str(parent.resolve()))
    return tuple(sorted(found))


def _normalize_state(state: str | None) -> str:
    """Uppercase a backing-store state (open/closed/merged) for the warm-cache."""
    return (state or "").strip().upper()


def _build_github_warm_cache(
    repository_host: "RepositoryHost",
    focus_issue_number: int | None,
) -> GithubWarmCache | None:
    """Best-effort GitHub ground-truth read; ``None`` on any failure.

    Uses only what the ``RepositoryHost`` port cheaply exposes:
    ``get_issue`` (state + labels) and ``get_prs_for_issue`` (number, state,
    merged, base/head refs, url). ``merge_commit_oid`` is not on ``PRInfo`` so
    it is left ``None``. ANY GitHub/network error is caught and logged — the
    warm-cache never fails the launch (the public repo lets the agent recover
    the same facts with local git).
    """
    if focus_issue_number is None:
        return None
    try:
        issue = repository_host.get_issue(focus_issue_number)
        issue_cache = (
            GithubIssueCache(
                number=issue.number,
                state=_normalize_state(issue.state),
                labels=tuple(issue.labels),
            )
            if issue is not None
            else None
        )
        prs = repository_host.get_prs_for_issue(focus_issue_number, state="all")
        branch_prefix = f"{focus_issue_number}-"
        pr_caches = tuple(
            GithubPrCache(
                number=pr.number,
                state=_normalize_state(pr.state),
                merged=(pr.state or "").strip().lower() == "merged",
                base_ref=pr.base_branch,
                head_ref=pr.branch or None,
                branch_matches_focus=(pr.branch or "").startswith(branch_prefix),
                merge_commit_oid=None,
                url=pr.url,
            )
            for pr in prs
        )
        return GithubWarmCache(issue=issue_cache, prs=pr_caches)
    except Exception as exc:  # noqa: BLE001 - best-effort warm-cache, never fatal
        logger.warning(
            "[triage] Evidence-map GitHub warm-cache unavailable for issue #%s: %s",
            focus_issue_number,
            exc,
        )
        return None


def build_evidence_map(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    focus_issue_number: int | None,
    artifact_hints: Sequence[str] = (),
) -> EvidenceMap:
    """Build the evidence map for a triage session.

    ``focus_issue_number`` is the failure-investigation focus (``None`` for a
    locations-only health-review map). Run-dirs and the GitHub warm-cache are
    only resolved when a focus issue is present; with none, the map carries
    just the orchestrator-state locations and a ``null`` ``github`` block.
    """
    return EvidenceMap(
        focus_issue_number=focus_issue_number,
        repo=config.repo or "",
        default_branch=_resolve_default_branch(config),
        state_dir=str(state_dir(config.repo_root)),
        orchestrator_log=str(get_repo_log_path(config.repo_root)),
        timeline_sqlite=str(state_dir(config.repo_root) / "timeline.sqlite"),
        run_dirs=_resolve_run_dirs(config, focus_issue_number, artifact_hints),
        github=_build_github_warm_cache(repository_host, focus_issue_number),
    )


def write_evidence_map(run_dir: Path, evidence: EvidenceMap) -> Path:
    """Write ``evidence-map.json`` into the run's triage-data directory."""
    path = run_dir / "triage-data" / EVIDENCE_MAP_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence.to_dict(), indent=2), encoding="utf-8")
    logger.info(
        "[triage] Evidence map written: %s (focus=%s, %d run-dir(s))",
        path,
        evidence.focus_issue_number,
        len(evidence.run_dirs),
    )
    return path
