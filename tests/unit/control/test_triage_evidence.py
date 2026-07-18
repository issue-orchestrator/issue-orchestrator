"""Tests for the triage evidence-map owner (control/triage_evidence.py)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.triage_evidence import (
    EVIDENCE_MAP_FILENAME,
    EVIDENCE_MAP_SCHEMA_VERSION,
    build_evidence_map,
    write_evidence_map,
)
from issue_orchestrator.infra.config import Config
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


def _make_sibling_run_dirs(tmp_path: Path, issue_number: int, names) -> list[Path]:
    """Create the sibling session worktree's run-dirs and return their paths."""
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


class _FakeRepositoryHost:
    """Minimal RepositoryHost stub exposing only the warm-cache reads."""

    def __init__(self, *, issue=None, prs=(), raises: bool = False) -> None:
        self._issue = issue
        self._prs = list(prs)
        self._raises = raises
        self.issue_calls: list[int] = []
        self.pr_calls: list[tuple[int, str]] = []

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
                number=6335, state="open", labels=["blocked-failed", "agent:triage"]
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

        assert evidence.schema_version == EVIDENCE_MAP_SCHEMA_VERSION
        assert evidence.focus_issue_number == 6335
        assert evidence.repo == "owner/name"
        assert evidence.default_branch == "main"
        state_dir = config.repo_root / ".issue-orchestrator" / "state"
        assert evidence.state_dir == str(state_dir)
        assert evidence.orchestrator_log == str(state_dir / "logs" / "orchestrator.log")
        assert evidence.timeline_sqlite == str(state_dir / "timeline.sqlite")
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
                    branch="triage-techlead-attention-sweep",
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
        assert evidence.run_dirs == ()
        assert evidence.focus_issue_number is None
        # No focus issue => the port is never queried.
        assert host.issue_calls == []
        assert host.pr_calls == []


class TestWriteEvidenceMap:
    def test_writes_json_matching_schema(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
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

        assert path == run_dir / "triage-data" / EVIDENCE_MAP_FILENAME
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["schema_version"] == 1
        assert data["focus_issue_number"] == 6335
        assert data["repo"] == "owner/name"
        assert data["default_branch"] == "main"
        assert set(data.keys()) == {
            "schema_version",
            "focus_issue_number",
            "repo",
            "default_branch",
            "state_dir",
            "orchestrator_log",
            "timeline_sqlite",
            "run_dirs",
            "github",
            "guidance",
        }
        assert data["github"]["issue"]["state"] == "OPEN"
        assert data["github"]["prs"] == []
        assert "PUBLIC" in data["guidance"]

    def test_locations_only_map_has_null_github(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        evidence = build_evidence_map(
            config=config,
            repository_host=_FakeRepositoryHost(),
            focus_issue_number=None,
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        data = json.loads(write_evidence_map(run_dir, evidence).read_text())

        assert data["github"] is None
        assert data["run_dirs"] == []
        assert data["focus_issue_number"] is None
        # Locations are still present.
        assert data["timeline_sqlite"].endswith("timeline.sqlite")
