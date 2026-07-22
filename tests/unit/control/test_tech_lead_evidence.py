"""Tests for the tech_lead evidence-map owner (control/tech_lead_evidence.py)."""

import json
from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.control.tech_lead_evidence import (
    EVIDENCE_MAP_FILENAME,
    EVIDENCE_MAP_SCHEMA_VERSION,
    build_evidence_map,
    write_evidence_map,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def _config(tmp_path: Path) -> Config:
    """A Config whose repo_root/worktree_base frame a sibling session worktree."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config = Config(repo="owner/name")
    config.repo_root = repo_root
    # worktrees.base resolves to repo_root.parent in production (base: "../").
    config.worktree_base = tmp_path
    return config


def _register_worktree(repo_root: Path, worktree: Path) -> Path:
    """Make ``worktree`` a REAL linked git worktree of ``repo_root``.

    Provenance is by shared git common dir (#6824 R4): the main repo gets a
    ``.git`` dir; the linked worktree gets a ``.git`` file pointing at
    ``<repo>/.git/worktrees/<name>`` whose ``commondir`` resolves back to the
    repo's ``.git``. A same-prefixed but UNRELATED sibling repo has its own
    ``.git`` and a different common dir, so it is excluded.
    """
    git_common = repo_root / ".git"
    git_common.mkdir(parents=True, exist_ok=True)
    wt_gitdir = git_common / "worktrees" / worktree.name
    wt_gitdir.mkdir(parents=True, exist_ok=True)
    (wt_gitdir / "commondir").write_text("../..\n")
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {wt_gitdir}\n")
    return worktree


def _make_sibling_run_dirs(tmp_path: Path, issue_number: int, names) -> list[Path]:
    """Create the sibling session worktree's run-dirs and return their paths.

    The sibling is registered as a real worktree (#6824 R4 provenance) so the
    focus-run-dir collector accepts it.
    """
    _register_worktree(tmp_path / "repo", tmp_path / f"repo-{issue_number}")
    sessions_dir = (
        tmp_path / f"repo-{issue_number}" / ".issue-orchestrator" / "sessions"
    )
    sessions_dir.mkdir(parents=True)
    created = []
    for name in names:
        run_dir = sessions_dir / name
        run_dir.mkdir()
        created.append(run_dir)
    return created


def _make_worktree_run_dir(tmp_path: Path, worktree_name: str, run_name: str) -> Path:
    """Create one run-dir under an arbitrary worktree's sessions dir."""
    run_dir = (
        tmp_path
        / worktree_name
        / ".issue-orchestrator"
        / "sessions"
        / run_name
    )
    run_dir.mkdir(parents=True)
    return run_dir


def _seed_sqlite_stores(config: Config) -> dict[str, Path]:
    """Drop state-dir *.sqlite stores + the e2e.db one level up (as prod does)."""
    state = state_dir(config.repo_root)
    state.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}
    for name in ("timeline.sqlite", "tech_lead_authority.sqlite", "foo.sqlite"):
        path = state / name
        path.write_bytes(b"")
        created[name] = path
    e2e = config.repo_root / ".issue-orchestrator" / "e2e.db"
    e2e.write_bytes(b"")
    created["e2e.db"] = e2e
    return created


def _sqlite_by_path(evidence) -> dict[str, object]:
    return {loc.path: loc for loc in evidence.locations if loc.kind == "sqlite"}


class _FakeRepositoryHost:
    """Minimal RepositoryHost stub exposing only the warm-cache reads."""

    def __init__(
        self, *, issue=None, prs=(), raises: bool = False, default_branch: str = "main"
    ) -> None:
        self._issue = issue
        self._prs = list(prs)
        self._raises = raises
        self._default_branch = default_branch
        self.issue_calls: list[int] = []
        self.pr_calls: list[tuple[int, str]] = []

    def get_default_branch(self) -> str:
        if self._raises:
            raise RuntimeError("github down")
        return self._default_branch

    def get_issue(self, issue_number: int):
        self.issue_calls.append(issue_number)
        if self._raises:
            raise RuntimeError("github down")
        return self._issue

    def get_prs_for_issue(self, issue_number: int, state: str = "open"):
        self.pr_calls.append((issue_number, state))
        if self._raises:
            raise RuntimeError("github down")
        return list(self._prs)


class TestBuildEvidenceMapLocations:
    def test_locations_run_dirs_and_repo_fields(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        run_dirs = _make_sibling_run_dirs(
            tmp_path, 6335, ["20260101T000000__coding-1", "20260101T000100__reviewer-1"]
        )
        host = _FakeRepositoryHost(
            issue=SimpleNamespace(
                number=6335, state="open", labels=["blocked-failed", "agent:tech-lead"]
            ),
            prs=[
                PRInfo(
                    number=6770,
                    title="Fix",
                    url="https://example/pr/6770",
                    branch="6335-fix",
                    body="",
                    state="merged",
                    labels=[],
                    base_branch="6593-base",
                )
            ],
        )

        evidence = build_evidence_map(
            config=config,
            repository_host=host,
            focus_issue_number=6335,
        )

        assert evidence.schema_version == EVIDENCE_MAP_SCHEMA_VERSION == 2
        assert evidence.focus_issue_number == 6335
        assert evidence.repo == "owner/name"
        assert evidence.default_branch == "main"
        assert evidence.run_dirs == tuple(
            sorted(str(d.resolve()) for d in run_dirs)
        )

    def test_default_branch_reads_override(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        config.worktree_base_branch_override = "develop"

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )

        assert evidence.default_branch == "develop"

    def test_default_branch_reads_real_non_main_default(self, tmp_path: Path) -> None:
        # R6 (#6824): a repo whose real default is NOT main resolves correctly —
        # the map must not fabricate main.
        config = _config(tmp_path)
        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(default_branch="trunk"),
            focus_issue_number=None,
        )
        assert evidence.default_branch == "trunk"

    def test_default_branch_unknown_on_lookup_failure(self, tmp_path: Path) -> None:
        # R6 (#6824): when the GitHub lookup FAILS, the branch is unknown (None),
        # NOT a fabricated main — the agent resolves it from local git instead.
        config = _config(tmp_path)
        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(raises=True),
            focus_issue_number=None,
        )
        assert evidence.default_branch is None

    def test_run_dirs_merge_artifact_hint_parents(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _make_sibling_run_dirs(tmp_path, 6335, ["20260101T000000__coding-1"])
        # A hint pointing at a file in a DIFFERENT dir the glob never sees.
        other_run = tmp_path / "elsewhere" / "run-x"
        other_run.mkdir(parents=True)
        hint = other_run / "run-audit.json"
        hint.write_text("{}")

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=6335,
            artifact_hints=[str(hint)],
        )

        assert str(other_run.resolve()) in evidence.run_dirs
        # The globbed run-dir is still present too.
        globbed = (
            tmp_path
            / "repo-6335"
            / ".issue-orchestrator"
            / "sessions"
            / "20260101T000000__coding-1"
        )
        assert str(globbed.resolve()) in evidence.run_dirs

    def test_missing_sibling_worktree_yields_empty_run_dirs(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)  # no sibling worktree created

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=6335,
        )

        assert evidence.run_dirs == ()


class TestGodViewSubstrate:
    """The open-ended location grant: roots + generic-glob store discovery."""

    def test_substrate_root_locations_present(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        # A REGISTERED managed worktree for THIS repo (repo_root.name == "repo");
        # an unrelated dir; and a same-PREFIXED but unrelated sibling repo.
        managed = _register_worktree(config.repo_root, tmp_path / "repo-42")
        unrelated = tmp_path / "tixmeup"
        unrelated.mkdir()
        # R4: `repo-private` matches the `repo-*` glob but is a SEPARATE repo
        # (its own .git dir) — provenance must exclude it.
        prefix_collision = tmp_path / "repo-private"
        (prefix_collision / ".git").mkdir(parents=True)

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )

        kinds = {loc.kind for loc in evidence.locations}
        assert {"dir", "log", "repo", "github"} <= kinds
        by_kind: dict[str, list] = {}
        for loc in evidence.locations:
            by_kind.setdefault(loc.kind, []).append(loc)

        # The main repo root (for git) is a first-class location.
        (repo_loc,) = by_kind["repo"]
        assert repo_loc.path == str(config.repo_root)

        dir_paths = {loc.path for loc in by_kind["dir"]}
        assert str(state_dir(config.repo_root)) in dir_paths
        # F9: the repo-specific REGISTERED managed worktree IS a root...
        assert str(managed) in dir_paths
        # ...but the shared worktrees.base PARENT is NOT (it holds sibling repos)...
        assert str(config.worktree_base) not in dir_paths
        # ...an unrelated dir is never granted...
        assert str(unrelated) not in dir_paths
        # ...and a same-prefixed UNRELATED sibling repo is excluded by provenance.
        assert str(prefix_collision) not in dir_paths

        # The orchestrator log is a log location under the state dir.
        (log_loc,) = by_kind["log"]
        assert log_loc.path.endswith("orchestrator.log")

        # GitHub is a root pointer alongside the warm-cache.
        (gh_loc,) = by_kind["github"]
        assert gh_loc.path == "owner/name"
        assert gh_loc.exists is True

    def test_glob_discovers_all_sqlite_including_future_and_e2e_db(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        created = _seed_sqlite_stores(config)

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )

        by_path = _sqlite_by_path(evidence)
        # Every seeded store is discovered — state-dir *.sqlite AND the e2e.db
        # one level up under .issue-orchestrator/.
        for path in created.values():
            assert str(path.resolve()) in by_path

        # A future/unknown store the code has never heard of still appears
        # (the GLOB, not a hint table, is the source of truth).
        foo = by_path[str(created["foo.sqlite"].resolve())]
        assert foo.exists is True
        assert foo.description.startswith("read-only SQLite store")

        # Known stores carry a cheap by-stem hint on top of the generic text.
        assert "event store" in by_path[str(created["timeline.sqlite"].resolve())].description
        assert "case-file" in by_path[
            str(created["tech_lead_authority.sqlite"].resolve())
        ].description
        assert "outcomes" in by_path[str(created["e2e.db"].resolve())].description

    def test_glob_failure_yields_valid_map_never_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A read/glob failure must degrade to a partial-but-valid map, not raise.
        config = _config(tmp_path)
        _seed_sqlite_stores(config)

        def _boom(self, pattern):
            raise OSError("unreadable directory")

        monkeypatch.setattr(Path, "glob", _boom)

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=6335,
        )

        # Still schema-valid and serializable...
        assert evidence.schema_version == 2
        json.dumps(evidence.to_dict())
        # ...root locations still resolved (they don't depend on glob)...
        assert {"dir", "log", "repo", "github"} <= {
            loc.kind for loc in evidence.locations
        }
        # ...glob-dependent discovery degraded to empty, not an exception.
        assert not any(loc.kind == "sqlite" for loc in evidence.locations)
        assert evidence.run_dirs == ()


class TestWholeSystemRunDirs:
    """Health review (no focus) enumerates run-dirs across the whole system."""

    def test_health_review_enumerates_run_dirs_across_worktrees(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        _register_worktree(config.repo_root, tmp_path / "repo-100")
        _register_worktree(config.repo_root, tmp_path / "repo-200")
        d1 = _make_worktree_run_dir(tmp_path, "repo-100", "20260101T000000__coding-1")
        d2 = _make_worktree_run_dir(tmp_path, "repo-200", "20260102T000000__reviewer-1")
        # The main repo's own sessions are part of the whole system too.
        d3 = _make_worktree_run_dir(tmp_path, "repo", "20260103T000000__health-1")

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )

        for run_dir in (d1, d2, d3):
            assert str(run_dir.resolve()) in evidence.run_dirs
        # Sorted, no duplicates.
        assert list(evidence.run_dirs) == sorted(set(evidence.run_dirs))

    def test_whole_system_excludes_unregistered_prefix_sibling(
        self, tmp_path: Path
    ) -> None:
        # R4 (#6824): a same-prefixed but UNRELATED sibling repo's run-dirs are
        # NOT swept into the whole-system health review.
        config = _config(tmp_path)
        sibling = tmp_path / "repo-private"
        (sibling / ".git").mkdir(parents=True)
        leaked = _make_worktree_run_dir(tmp_path, "repo-private", "20260101T000000__x")

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )

        assert str(leaked.resolve()) not in evidence.run_dirs


class TestSandboxReadRoots:
    """R5 (#6824): the evidence map's typed read-root projection for the sandbox."""

    def test_projects_dir_repo_and_run_dir_roots_only(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _register_worktree(config.repo_root, tmp_path / "repo-6335")
        _make_sibling_run_dirs(tmp_path, 6335, ["20260101T000000__coding-1"])

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=6335,
        )

        roots = evidence.sandbox_read_roots()
        # The main repo (kind=repo) and state dir (kind=dir) are read roots...
        assert config.repo_root in roots
        assert state_dir(config.repo_root) in roots
        # ...run-dirs are granted...
        assert any("20260101T000000__coding-1" in str(r) for r in roots)
        # ...the github slug pointer and the log FILE are NOT filesystem roots.
        assert all(r != Path("owner/name") for r in roots)
        assert all(not str(r).endswith("orchestrator.log") for r in roots)
        # De-duplicated.
        assert len(roots) == len(set(roots))


class TestManagedWorktreeSelection:
    """R4 (#6824): registered-worktree provenance (uncapped) vs bounded mtime cap."""

    def test_focus_authorization_is_decoupled_from_the_cap(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # R4 (#6824): a focused investigation must keep its OWN registered run
        # history even when the whole-system enumeration cap is full of newer,
        # unrelated registered worktrees. Authorization != capped selection.
        import os

        import issue_orchestrator.control.tech_lead_evidence as te

        monkeypatch.setattr(te, "_MAX_WORKTREE_ROOTS", 1)
        config = _config(tmp_path)
        # A newer unrelated registered worktree fills the cap-of-1...
        _register_worktree(config.repo_root, tmp_path / "repo-99")
        os.utime(tmp_path / "repo-99", (9000, 9000))
        # ...while the OLDER focus worktree (repo-42) is also registered.
        _register_worktree(config.repo_root, tmp_path / "repo-42")
        os.utime(tmp_path / "repo-42", (1000, 1000))
        focus_run = _make_worktree_run_dir(tmp_path, "repo-42", "20260101T000000__c")

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=42,
        )

        # The focus run-dir is included despite the cap being filled by repo-99.
        assert str(focus_run.resolve()) in evidence.run_dirs

    def test_cap_keeps_most_recently_modified(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # R4 (#6824): the cap keeps the actually-newest by MTIME, not the highest
        # issue number — a freshly recreated low-number worktree must not lose to
        # an old high-number one.
        import os

        import issue_orchestrator.control.tech_lead_evidence as te

        monkeypatch.setattr(te, "_MAX_WORKTREE_ROOTS", 2)
        config = _config(tmp_path)
        for n in (9, 10, 100):
            _register_worktree(config.repo_root, tmp_path / f"repo-{n}")
        # Make the LOW-number worktree the most recent, the high one the oldest.
        os.utime(tmp_path / "repo-100", (1000, 1000))
        os.utime(tmp_path / "repo-10", (2000, 2000))
        os.utime(tmp_path / "repo-9", (3000, 3000))

        kept = {p.name for p in te._registered_managed_worktrees(config)}
        assert kept == {"repo-9", "repo-10"}  # newest by mtime, not numeric

    def test_focus_run_dirs_exclude_unrelated_prefix_sibling(
        self, tmp_path: Path
    ) -> None:
        # R4 (#6824): the FOCUSED-issue path must not disclose a same-named but
        # unrelated sibling repo's run-dirs.
        config = _config(tmp_path)
        sibling = tmp_path / "repo-42"
        (sibling / ".git").mkdir(parents=True)  # its OWN repo, not registered
        leaked = _make_worktree_run_dir(tmp_path, "repo-42", "20260101T000000__x")

        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=42,
        )

        assert str(leaked.resolve()) not in evidence.run_dirs


class TestBuildEvidenceMapGithubWarmCache:
    def test_populates_issue_and_prs(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        host = _FakeRepositoryHost(
            issue=SimpleNamespace(number=6335, state="open", labels=["blocked-failed"]),
            prs=[
                PRInfo(
                    number=6770,
                    title="Fix",
                    url="https://example/pr/6770",
                    branch="6335-fix",
                    body="",
                    state="merged",
                    labels=[],
                    base_branch="6593-base",
                )
            ],
        )

        evidence = build_evidence_map(
            config=config, repository_host=host, focus_issue_number=6335
        )

        assert evidence.github is not None
        assert evidence.github.issue is not None
        assert evidence.github.issue.number == 6335
        assert evidence.github.issue.state == "OPEN"
        assert evidence.github.issue.labels == ("blocked-failed",)
        (pr,) = evidence.github.prs
        assert pr.number == 6770
        assert pr.state == "MERGED"
        assert pr.merged is True
        assert pr.base_ref == "6593-base"
        assert pr.head_ref == "6335-fix"
        assert pr.branch_matches_focus is True  # "<issue>-<slug>" == this issue's PR
        assert pr.merge_commit_oid is None  # PRInfo does not carry it
        assert pr.url == "https://example/pr/6770"
        # The warm-cache read PRs in "all" state (terminal PRs are the point).
        assert host.pr_calls == [(6335, "all")]

    def test_referencing_pr_is_flagged_not_focus_branch(self, tmp_path: Path) -> None:
        # A PR whose head branch is not "<issue>-..." only references the issue
        # (the real #6335/#6824 case: the rework PR matched by title-contains).
        # It is surfaced but flagged branch_matches_focus=False so it cannot be
        # mistaken for the issue's own implementation.
        config = _config(tmp_path)
        host = _FakeRepositoryHost(
            issue=SimpleNamespace(number=6335, state="open", labels=["blocked-failed"]),
            prs=[
                PRInfo(
                    number=6824,
                    title="Rework referencing #6335",
                    url="https://example/pr/6824",
                    branch="tech-lead-techlead-attention-sweep",
                    body="",
                    state="open",
                    labels=[],
                    base_branch="main",
                )
            ],
        )

        evidence = build_evidence_map(
            config=config, repository_host=host, focus_issue_number=6335
        )

        (pr,) = evidence.github.prs
        assert pr.number == 6824
        assert pr.branch_matches_focus is False

    def test_best_effort_null_when_host_raises(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        run_dirs = _make_sibling_run_dirs(
            tmp_path, 6335, ["20260101T000000__coding-1"]
        )
        host = _FakeRepositoryHost(raises=True)

        # Must NOT propagate: github is null, everything else still resolved.
        evidence = build_evidence_map(
            config=config, repository_host=host, focus_issue_number=6335
        )

        assert evidence.github is None
        assert evidence.run_dirs == tuple(str(d.resolve()) for d in run_dirs)

    def test_null_github_for_no_focus_issue(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        host = _FakeRepositoryHost(
            issue=SimpleNamespace(number=1, state="open", labels=[])
        )

        evidence = build_evidence_map(
            config=config, repository_host=host, focus_issue_number=None
        )

        assert evidence.github is None
        assert evidence.run_dirs == ()  # no sessions anywhere in this tmp system
        assert evidence.focus_issue_number is None
        # No focus issue => the port is never queried.
        assert host.issue_calls == []
        assert host.pr_calls == []


class TestWriteEvidenceMap:
    def test_writes_json_matching_schema(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _seed_sqlite_stores(config)
        host = _FakeRepositoryHost(
            issue=SimpleNamespace(number=6335, state="open", labels=["blocked-failed"]),
            prs=[],
        )
        evidence = build_evidence_map(
            config=config, repository_host=host, focus_issue_number=6335
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        path = write_evidence_map(run_dir, evidence)

        assert path == run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["schema_version"] == 2
        assert data["focus_issue_number"] == 6335
        assert data["repo"] == "owner/name"
        assert data["default_branch"] == "main"
        assert set(data.keys()) == {
            "schema_version",
            "focus_issue_number",
            "repo",
            "default_branch",
            "locations",
            "run_dirs",
            "github",
            "guidance",
        }
        # Each location serializes as a typed record.
        for loc in data["locations"]:
            assert set(loc.keys()) == {"path", "kind", "description", "exists"}
            assert loc["kind"] in {"dir", "sqlite", "log", "repo", "github"}
        kinds = {loc["kind"] for loc in data["locations"]}
        assert {"dir", "log", "repo", "github", "sqlite"} <= kinds
        assert data["github"]["issue"]["state"] == "OPEN"
        assert data["github"]["prs"] == []
        # Guidance keeps the PUBLIC-repo verification cue and the open-ended
        # file-an-issue-to-instrument mandate.
        assert "PUBLIC" in data["guidance"]
        assert "file an issue to instrument" in data["guidance"]

    def test_health_review_map_has_null_github_and_whole_system_locations(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        _seed_sqlite_stores(config)
        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        data = json.loads(write_evidence_map(run_dir, evidence).read_text())

        assert data["github"] is None
        assert data["focus_issue_number"] is None
        # Even with no focus, the god-view substrate (stores + roots) is present.
        assert any(loc["kind"] == "sqlite" for loc in data["locations"])
        assert any(loc["kind"] == "repo" for loc in data["locations"])
