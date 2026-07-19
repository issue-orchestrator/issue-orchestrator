"""Owner for the triage EVIDENCE MAP — a god-view location manifest + warm-cache.

The triage tech lead (ADR-0031) receives ``board-snapshot.json`` today. That
leaves it blind to the raw evidence a real investigation reasons from: the
session run-dirs, the orchestrator log, the SQLite stores (timeline events, E2E
outcomes, the triage case-file ledger, and anything added later), local ``git``
in the main repo, and GitHub ground truth. This module builds and writes an
``evidence-map.json`` next to the board snapshot.

The lever is ACCESS, not prose: rather than enumerate today's known leaves, the
map grants the SUBSTRATE — a small set of roots (the state dir, the orchestrator
log, the main repo, the session-worktrees root, GitHub) plus a generic glob that
DISCOVERS every ``*.sqlite`` / ``*.db`` store under the orchestrator data dir.
Anything we instrument later shows up for free with zero re-plumbing, and when a
signal the tech lead needs is not instrumented yet the correct behavior is for IT
to file an issue to instrument it — not for us to pre-build sensors. Writes stay
unchanged — still gated / orchestrator-executed; this only stages read-side
evidence.

Best-effort by design (a deliberate exception to the repo's fail-fast house
style): unlike the board snapshot, the evidence map is an ENHANCEMENT, never a
required input, so a failure to build or write it must NOT fail the session
launch. Filesystem discovery (globs, ``exists`` probes) tolerates ``OSError`` and
degrades to a partial-but-valid map rather than raising; the GitHub warm-cache is
doubly best-effort — any port/network error yields a ``null`` ``github`` block —
because the public repo lets the agent verify everything itself with local
``git``. The call-site wrapper (``triage_session_policy._stage_evidence_map``)
owns the outer catch as a final backstop.
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

EVIDENCE_MAP_SCHEMA_VERSION = 2

# Canonical evidence-map filename inside a session's triage-data directory,
# next to BOARD_SNAPSHOT_FILENAME (domain/board_snapshot.py).
EVIDENCE_MAP_FILENAME = "evidence-map.json"

# Sub-directory under a session worktree that holds run-dirs. Mirrors
# ``execution.session_output_adapter.SESSION_OUTPUT_DIR`` ("sessions"); the
# literal is duplicated here to keep this control-layer owner off an
# execution-adapter import.
_SESSIONS_SUBDIR = "sessions"

# Glob patterns that DISCOVER SQLite stores by substrate, not by name. State
# stores live directly under the state dir as ``*.sqlite``
# (timeline/triage_authority/label_store/queue_cache/session_registry/… — see
# infra/sqlite_registry.py); the E2E store is ``e2e.db`` one level up, directly
# under the orchestrator data dir. Both suffixes are globbed in BOTH roots so
# any future store in either place, under either suffix, is found for free.
# Top-level (non-recursive) globs keep this bounded — they never descend into
# per-session run-dirs or backup subdirs.
_DB_SUFFIX_GLOBS = ("*.sqlite", "*.db")

# Cheap by-filename hints layered on top of the generic description. The GLOB is
# the source of truth — an unknown/future store still appears, just without a
# hint — so this map only annotates the ones we happen to recognize by stem.
_DB_DESCRIPTION_HINTS = {
    "timeline": "event store (agent/session timeline events)",
    "e2e": "E2E run + per-test outcomes and durations",
    "triage_authority": "triage case-file / pattern / shipped-fix ledger",
}

# Upper bound on run-dirs listed in a whole-system health-review map. A
# long-lived install with many worktrees × sessions could otherwise bloat the
# map; when more exist the NEWEST are kept (run-dir names are timestamp-prefixed)
# and the truncation is logged — never a silent cap.
_MAX_RUN_DIRS = 200

# Default branch the agent verifies merge-reachability against. GitHub repos
# default to ``main``; a repo whose default differs sets it explicitly via
# ``worktrees.base_branch_override`` (config.worktree_base_branch_override),
# which is the same override the worktree base-branch resolver keys on. Reading
# the true default over git would need a git port this launch-prep path does
# not carry — the guidance text tells the agent to confirm with local git.
_DEFAULT_BRANCH_FALLBACK = "main"

_GUIDANCE = (
    "These are ROOTS, not a fixed inventory. You have READ access to EVERYTHING "
    "under them, including artifacts created AFTER this map was written — "
    "enumerate and explore them, don't stop at what is listed. List the state "
    "dir to find every store; open any *.sqlite/*.db with sqlite3 "
    "(timeline=events, e2e=run/test outcomes+durations, "
    "triage_authority=case-file/pattern/shipped-fix ledger, plus any store added "
    "later); walk the run-dirs; run git in the repo root (log, blame, merge-base, "
    "conflict/rebase history). For a failure investigation: read the run-dir "
    "artifacts (run-audit.json, validation-record.json, completion-record.json, "
    "analysis.json) and key on validation.passed, NOT the outcome string. This "
    "repo is PUBLIC: verify merge-reachability with local git (e.g. `git -C "
    "<run_dir> fetch origin --quiet && git merge-base --is-ancestor "
    "<merge_commit_oid> origin/<default_branch>`). In github.prs, "
    "branch_matches_focus=true is THIS issue's own implementation PR; "
    "branch_matches_focus=false only references the issue (e.g. a meta/rework PR) "
    "and is NOT its implementation - do not treat it as the issue's work. The "
    "github block is a best-effort warm-cache; when it is null or thin, gather "
    "the rest yourself with git. If a signal you need to judge system health is "
    "not instrumented yet, that gap is itself a finding: file an issue to "
    "instrument it (a can-wait item for the issue queue) rather than guessing."
)


@dataclass(frozen=True)
class EvidenceLocation:
    """One god-view root (or discovered store) the tech lead may read.

    ``path`` is an absolute path string (or, for ``kind == "github"``, the repo
    slug pointer). ``kind`` is one of ``"dir" | "sqlite" | "log" | "repo" |
    "github"``. ``exists`` is a best-effort filesystem probe at build time (a
    root can legitimately be absent on a fresh install) — always ``True`` for a
    ``sqlite`` location because it was just discovered by glob, and for
    ``github`` when a repo slug is configured.
    """

    path: str
    kind: str
    description: str
    exists: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "description": self.description,
            "exists": self.exists,
        }


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
    """A triage session's read-side evidence manifest (schema_version 2).

    ``locations`` is the god-view SUBSTRATE: the roots the tech lead may explore
    (state dir, orchestrator log, main repo, session-worktrees root, GitHub) plus
    every ``*.sqlite`` / ``*.db`` store DISCOVERED under the orchestrator data
    dir — an open-ended grant, not an enumerated list, so future stores appear
    for free. ``run_dirs`` is the distinct enumeration of per-session run
    directories: the focus issue's own runs for a failure investigation, or
    whole-system runs across every worktree for a health review (bounded by
    :data:`_MAX_RUN_DIRS`). ``github`` is ``None`` for a health review or when
    the warm-cache read failed; ``focus_issue_number`` is ``None`` when there is
    no single focus issue.
    """

    focus_issue_number: int | None
    repo: str
    default_branch: str
    locations: tuple[EvidenceLocation, ...]
    run_dirs: tuple[str, ...]
    github: GithubWarmCache | None
    schema_version: int = EVIDENCE_MAP_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "focus_issue_number": self.focus_issue_number,
            "repo": self.repo,
            "default_branch": self.default_branch,
            "locations": [loc.to_dict() for loc in self.locations],
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


def _worktrees_root(config: "Config") -> Path:
    """The directory that holds the ``<repo>-<n>`` session worktrees.

    Mirrors ``adapters.worktree._worktree``: ``worktree_base`` (config resolves
    ``worktrees.base`` to an absolute path at load time) or, unset, the main
    repo's parent, where the sibling worktrees live.
    """
    repo_root = Path(config.repo_root)
    return Path(config.worktree_base) if config.worktree_base else repo_root.parent


def _session_worktree(config: "Config", focus_issue_number: int) -> Path:
    """The orchestrator-managed session worktree, a sibling of the main repo.

    ``<worktree_base>/<repo_root.name>-<issue_number>``.
    """
    return _worktrees_root(config) / f"{Path(config.repo_root).name}-{focus_issue_number}"


def _path_exists(path: Path) -> bool:
    """Best-effort ``exists`` probe that never raises (OSError -> False)."""
    try:
        return path.exists()
    except OSError:
        return False


def _sqlite_description(stem: str) -> str:
    """Generic store description, annotated by a cheap by-stem hint when known."""
    base = "read-only SQLite store; query with sqlite3"
    hint = _DB_DESCRIPTION_HINTS.get(stem)
    return f"{base} — {hint}" if hint else base


def _discover_sqlite_locations(state_dir_path: Path) -> list[EvidenceLocation]:
    """Discover every SQLite store as an :class:`EvidenceLocation` by glob.

    Globs ``*.sqlite`` / ``*.db`` in the state dir AND the orchestrator data dir
    (its parent, where ``e2e.db`` lives), so timeline + triage_authority + e2e +
    any store added later are all found without an enumerated list. Best-effort:
    a glob that raises ``OSError`` (unreadable dir) is skipped, not fatal.
    """
    data_root = state_dir_path.parent  # .issue-orchestrator/
    discovered: dict[str, EvidenceLocation] = {}
    for root in (state_dir_path, data_root):
        for pattern in _DB_SUFFIX_GLOBS:
            try:
                matches = sorted(root.glob(pattern))
            except OSError as exc:
                logger.warning(
                    "[triage] Evidence-map SQLite glob %s in %s failed: %s",
                    pattern,
                    root,
                    exc,
                )
                continue
            for db in matches:
                if not db.is_file():
                    continue
                key = str(db.resolve())
                if key in discovered:
                    continue
                discovered[key] = EvidenceLocation(
                    path=key,
                    kind="sqlite",
                    description=_sqlite_description(db.stem),
                    exists=True,
                )
    return [discovered[key] for key in sorted(discovered)]


def _build_locations(config: "Config") -> tuple[EvidenceLocation, ...]:
    """The god-view substrate: root pointers + every discovered SQLite store."""
    state_dir_path = state_dir(config.repo_root)
    log_path = get_repo_log_path(config.repo_root)
    repo_root = Path(config.repo_root)
    worktrees_root = _worktrees_root(config)
    repo_slug = config.repo or ""
    roots = [
        EvidenceLocation(
            path=str(state_dir_path),
            kind="dir",
            description=(
                "orchestrator state dir — SQLite stores, logs, caches; list it "
                "to discover every store, including ones added after this map"
            ),
            exists=_path_exists(state_dir_path),
        ),
        EvidenceLocation(
            path=str(log_path),
            kind="log",
            description=(
                "orchestrator log (rotated siblings in the same dir); grep for "
                "issue/session failure signatures"
            ),
            exists=_path_exists(log_path),
        ),
        EvidenceLocation(
            path=str(repo_root),
            kind="repo",
            description=(
                "main repo working copy — run git here (log, blame, merge-base, "
                "conflict/rebase history)"
            ),
            exists=_path_exists(repo_root),
        ),
        EvidenceLocation(
            path=str(worktrees_root),
            kind="dir",
            description=(
                "session-worktrees root — the <repo>-<n> agent worktrees and "
                "their .issue-orchestrator/sessions run-dirs live under here"
            ),
            exists=_path_exists(worktrees_root),
        ),
        EvidenceLocation(
            path=repo_slug or "(unknown)",
            kind="github",
            description=(
                "GitHub ground truth (issues/PRs/CI); a warm-cache is in the "
                "`github` block — confirm live state with local git"
            ),
            exists=bool(repo_slug),
        ),
    ]
    return tuple(roots) + tuple(_discover_sqlite_locations(state_dir_path))


def _session_run_dirs_under(worktree_root: Path) -> list[Path]:
    """The ``*__*`` run-dirs under one worktree's sessions dir (empty if none)."""
    sessions_dir = worktree_root / ".issue-orchestrator" / _SESSIONS_SUBDIR
    if not sessions_dir.is_dir():
        return []
    return [entry for entry in sessions_dir.glob("*__*") if entry.is_dir()]


def _collect_focus_run_dirs(
    config: "Config",
    focus_issue_number: int,
    artifact_hints: Sequence[str],
    found: set[str],
) -> None:
    """Focus-issue run-dirs: its sibling worktree's runs + artifact-hint parents."""
    for entry in _session_run_dirs_under(_session_worktree(config, focus_issue_number)):
        found.add(str(entry.resolve()))
    for hint in artifact_hints:
        if hint:
            found.add(str(Path(hint).parent.resolve()))


def _collect_whole_system_run_dirs(config: "Config", found: set[str]) -> None:
    """Whole-system run-dirs (health review): every worktree's session runs.

    Enumerates the main repo plus every ``<repo>-*`` sibling worktree under the
    worktrees root, so a health review sees the whole floor rather than one
    focus. Bounding to the newest happens in the shared finalizer.
    """
    repo_root = Path(config.repo_root)
    worktrees_root = _worktrees_root(config)
    roots = {repo_root}
    if worktrees_root.is_dir():
        for entry in worktrees_root.glob(f"{repo_root.name}-*"):
            if entry.is_dir():
                roots.add(entry)
    for root in roots:
        for entry in _session_run_dirs_under(root):
            found.add(str(entry.resolve()))


def _bounded_run_dirs(found: set[str]) -> tuple[str, ...]:
    """Sorted run-dirs, capped at :data:`_MAX_RUN_DIRS` keeping the newest.

    Run-dir names are timestamp-prefixed, so newest == reverse lexical on the
    basename. Truncation is logged (no silent cap); the result is re-sorted for
    a stable, diff-friendly map.
    """
    if len(found) <= _MAX_RUN_DIRS:
        return tuple(sorted(found))
    newest = sorted(found, key=lambda p: Path(p).name, reverse=True)[:_MAX_RUN_DIRS]
    logger.warning(
        "[triage] Evidence-map run-dirs truncated to %d of %d (newest kept)",
        _MAX_RUN_DIRS,
        len(found),
    )
    return tuple(sorted(newest))


def _resolve_run_dirs(
    config: "Config",
    focus_issue_number: int | None,
    artifact_hints: Sequence[str],
) -> tuple[str, ...]:
    """Absolute, de-duped, sorted, bounded run-dir paths for this triage session.

    Focus present (failure investigation): the focus issue's own runs plus any
    ``artifact_hints`` parents. Focus absent (health review): whole-system runs
    across every worktree. Best-effort: an ``OSError`` mid-discovery returns
    whatever was collected so far rather than raising.
    """
    found: set[str] = set()
    try:
        if focus_issue_number is not None:
            _collect_focus_run_dirs(config, focus_issue_number, artifact_hints, found)
        else:
            _collect_whole_system_run_dirs(config, found)
    except OSError as exc:
        logger.warning(
            "[triage] Evidence-map run-dir discovery failed (partial result): %s",
            exc,
        )
    return _bounded_run_dirs(found)


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
    health-review map). Every map carries the full god-view substrate
    (``locations``); a focus adds its own run-dirs + a GitHub warm-cache, while
    a health review enumerates whole-system run-dirs across every worktree and
    leaves ``github`` ``None``.
    """
    return EvidenceMap(
        focus_issue_number=focus_issue_number,
        repo=config.repo or "",
        default_branch=_resolve_default_branch(config),
        locations=_build_locations(config),
        run_dirs=_resolve_run_dirs(config, focus_issue_number, artifact_hints),
        github=_build_github_warm_cache(repository_host, focus_issue_number),
    )


def write_evidence_map(run_dir: Path, evidence: EvidenceMap) -> Path:
    """Write ``evidence-map.json`` into the run's triage-data directory."""
    path = run_dir / "triage-data" / EVIDENCE_MAP_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence.to_dict(), indent=2), encoding="utf-8")
    logger.info(
        "[triage] Evidence map written: %s (focus=%s, %d location(s), %d run-dir(s))",
        path,
        evidence.focus_issue_number,
        len(evidence.locations),
        len(evidence.run_dirs),
    )
    return path
