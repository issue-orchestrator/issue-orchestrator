"""Tests for the ADR-0031 tech_lead session policy owner."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control import tech_lead_session_policy
from issue_orchestrator.control.completion_pr_collision import NoCommitsBetweenError
from issue_orchestrator.control.tech_lead_evidence import EVIDENCE_MAP_FILENAME
from issue_orchestrator.control.tech_lead_session_policy import (
    _stage_evidence_map,
    is_benign_tech_lead_no_commits,
    is_tech_lead_session,
    read_tech_lead_assignment,
    shape_requested_actions_for_tech_lead,
)
from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadSessionFlavor,
)


class TestIsTechLeadSession:
    @pytest.mark.parametrize(
        ("tech_lead_agent", "agent_type", "expected"),
        [
            ("agent:tech-lead", "agent:tech-lead", True),
            ("agent:tech-lead", "agent:web", False),
            ("agent:tech-lead", None, False),
            (None, "agent:tech-lead", False),
            (None, None, False),
            ("", "agent:tech-lead", False),
            ("", "", False),
        ],
    )
    def test_matrix(
        self, tech_lead_agent: str | None, agent_type: str | None, expected: bool
    ) -> None:
        assert is_tech_lead_session(tech_lead_agent, agent_type) is expected


class TestShapeRequestedActionsForTechLead:
    def test_drops_only_post_comment(self) -> None:
        requested = (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.CREATE_PR,
            RequestedAction.POST_COMMENT,
        )

        shaped = shape_requested_actions_for_tech_lead(requested)

        assert shaped == (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR)

    def test_preserves_order_and_other_actions(self) -> None:
        requested = (
            RequestedAction.POST_COMMENT,
            RequestedAction.PUSH_BRANCH,
            RequestedAction.ADD_BLOCKED_LABEL,
            RequestedAction.POST_COMMENT,
        )

        shaped = shape_requested_actions_for_tech_lead(requested)

        assert shaped == (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.ADD_BLOCKED_LABEL,
        )

    def test_no_post_comment_is_identity(self) -> None:
        requested = (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR)

        assert shape_requested_actions_for_tech_lead(requested) == requested


class TestIsBenignTechLeadNoCommits:
    def test_true_only_for_create_pr_with_no_commits_error(self) -> None:
        error = NoCommitsBetweenError(base="main", head="issue-1")

        assert is_benign_tech_lead_no_commits(RequestedAction.CREATE_PR, error) is True

    @pytest.mark.parametrize(
        "action",
        [a for a in RequestedAction if a is not RequestedAction.CREATE_PR],
    )
    def test_false_for_other_actions(self, action: RequestedAction) -> None:
        error = NoCommitsBetweenError(base="main", head="issue-1")

        assert is_benign_tech_lead_no_commits(action, error) is False

    def test_false_for_other_errors_on_create_pr(self) -> None:
        assert (
            is_benign_tech_lead_no_commits(
                RequestedAction.CREATE_PR, RuntimeError("boom")
            )
            is False
        )


class TestReadTechLeadAssignment:
    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert read_tech_lead_assignment(tmp_path) is None

    def test_reads_assignment_from_tech_lead_data(self, tmp_path: Path) -> None:
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=99,
            focus_reason="hang",
        )
        assignment.write(tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME)

        assert read_tech_lead_assignment(tmp_path) == assignment

    def test_malformed_content_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1, "flavor": "bogus"}')

        with pytest.raises(ValueError, match="flavor"):
            read_tech_lead_assignment(tmp_path)


class TestDiscardTechLeadAuthorityAfterCompletion:
    """The retention owner drops BOTH tech_lead records at a run's terminal.

    The run-keyed launch authority and the anchor-keyed storm cohort (#6780)
    have different keys but the same end: once the review's run is over,
    neither may outlive it. The cohort's discard is what releases its members'
    held run artifacts for cleanup.
    """

    @staticmethod
    def _config():
        from issue_orchestrator.infra.config import Config

        config = Config(repo="test/repo")
        config.tech_lead_review_agent = "agent:tech-lead"
        return config

    @staticmethod
    def _session(agent_type: str):
        from unittest.mock import MagicMock

        session = MagicMock()
        session.issue.number = 999
        session.issue.agent_type = agent_type
        session.run_assets.run_id = "r1"
        session.run_assets.session_name = "issue-999"
        return session

    @staticmethod
    def _store_with_both_rows():
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        store = InMemoryTechLeadAuthorityStore()
        store.record(
            run_id="r1",
            session_name="issue-999",
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
                anchor_issue_number=999,
                problem_issue_numbers=(41, 42),
            ),
        )
        store.record_storm_cohort(
            anchor_issue_number=999,
            cohort=tuple(
                DiscoveredFailure(number, f"Problem {number}", "failed")
                for number in (41, 42)
            ),
        )
        return store

    def test_terminal_completion_discards_authority_and_cohort(self) -> None:
        from issue_orchestrator.control.tech_lead_completion import (
            discard_tech_lead_authority_after_completion,
        )

        store = self._store_with_both_rows()

        discard_tech_lead_authority_after_completion(
            self._config(),
            store,
            self._session("agent:tech-lead"),
            processing_errors=None,
        )

        assert store.load(run_id="r1", session_name="issue-999") is None
        assert store.load_storm_cohort(anchor_issue_number=999) is None
        assert store.list_storm_cohorts() == ()

    def test_publish_failure_retains_both_for_the_retry(self) -> None:
        """A publish-stage failure re-enters completion for this same run, so
        neither record may be dropped yet."""
        from issue_orchestrator.control.completion_types import ERROR_PREFIX_PUSH
        from issue_orchestrator.control.tech_lead_completion import (
            discard_tech_lead_authority_after_completion,
        )

        store = self._store_with_both_rows()

        discard_tech_lead_authority_after_completion(
            self._config(),
            store,
            self._session("agent:tech-lead"),
            processing_errors=[f"{ERROR_PREFIX_PUSH}: remote rejected"],
        )

        assert store.load(run_id="r1", session_name="issue-999") is not None
        assert store.load_storm_cohort(anchor_issue_number=999) is not None

    def test_non_tech_lead_session_touches_nothing(self) -> None:
        from issue_orchestrator.control.tech_lead_completion import (
            discard_tech_lead_authority_after_completion,
        )

        store = self._store_with_both_rows()

        discard_tech_lead_authority_after_completion(
            self._config(),
            store,
            self._session("agent:coder"),
            processing_errors=None,
        )

        assert store.load(run_id="r1", session_name="issue-999") is not None
        assert store.load_storm_cohort(anchor_issue_number=999) is not None


class TestStageEvidenceMap:
    """The evidence-map wiring: flavor gating, best-effort, manifest recording."""

    @staticmethod
    def _config(tmp_path: Path) -> SimpleNamespace:
        repo_root = tmp_path / "repo"
        repo_root.mkdir(exist_ok=True)
        return SimpleNamespace(
            repo_root=repo_root,
            repo="owner/repo",
            worktree_base=tmp_path,
            worktree_base_branch_override=None,
        )

    @staticmethod
    def _ctx() -> tuple[SimpleNamespace, dict]:
        manifest: dict = {}
        return SimpleNamespace(update_manifest=manifest.update), manifest

    @staticmethod
    def _register_worktree(repo_root: Path, worktree: Path) -> None:
        """Make ``worktree`` a real linked git worktree of ``repo_root`` (#6824 R4)."""
        git_common = repo_root / ".git"
        git_common.mkdir(parents=True, exist_ok=True)
        wt_gitdir = git_common / "worktrees" / worktree.name
        wt_gitdir.mkdir(parents=True, exist_ok=True)
        (wt_gitdir / "commondir").write_text("../..\n")
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text(f"gitdir: {wt_gitdir}\n")

    @staticmethod
    def _host() -> SimpleNamespace:
        return SimpleNamespace(
            get_default_branch=lambda: "main",
            get_issue=lambda n: SimpleNamespace(
                number=n, state="open", labels=["blocked-failed"]
            ),
            get_prs_for_issue=lambda n, state="open": [
                SimpleNamespace(
                    number=6770,
                    state="merged",
                    base_branch="6593-predecessor",
                    branch="6335-work",
                    url="https://example/pr/6770",
                )
            ],
        )

    @staticmethod
    def _board(recent_failures: tuple = ()) -> SimpleNamespace:
        return SimpleNamespace(recent_failures=list(recent_failures))

    def test_failure_investigation_writes_map_and_records_manifest(
        self, tmp_path: Path
    ) -> None:
        ctx, manifest = self._ctx()
        run_dir = tmp_path / "run"
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=self._host(),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=6335,
            board_snapshot=self._board(),
        )
        path = run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME
        assert path.is_file()
        assert manifest["evidence_map"] == str(path)
        data = json.loads(path.read_text())
        assert data["focus_issue_number"] == 6335
        assert data["github"]["issue"]["state"] == "OPEN"
        pr = data["github"]["prs"][0]
        assert pr["merged"] is True
        assert pr["base_ref"] == "6593-predecessor"

    def test_batch_review_stages_nothing(self, tmp_path: Path) -> None:
        ctx, manifest = self._ctx()
        run_dir = tmp_path / "run"
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=self._host(),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            focus_issue_number=None,
            board_snapshot=self._board(),
        )
        assert not (run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME).exists()
        assert "evidence_map" not in manifest

    def test_health_review_stages_whole_system_map(self, tmp_path: Path) -> None:
        # A health review has no focus, so it gets the full SYSTEM substrate:
        # a null github block, but run-dirs enumerated across ALL worktrees.
        ctx, _manifest = self._ctx()
        config = self._config(tmp_path)
        run_dir = tmp_path / "run"
        # R4 (#6824): only REGISTERED worktrees of this repo are swept.
        self._register_worktree(config.repo_root, tmp_path / "repo-100")
        whole_system = (
            tmp_path
            / "repo-100"
            / ".issue-orchestrator"
            / "sessions"
            / "20260101T000000__coding-1"
        )
        whole_system.mkdir(parents=True)
        _stage_evidence_map(
            config=config,
            repository_host=self._host(),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            focus_issue_number=None,
            board_snapshot=self._board(),
        )
        data = json.loads(
            (run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME).read_text()
        )
        assert data["focus_issue_number"] is None
        assert data["github"] is None
        # Whole-system run-dirs are enumerated across worktrees, not empty.
        assert str(whole_system.resolve()) in data["run_dirs"]

    def test_github_read_failure_does_not_fail_launch(self, tmp_path: Path) -> None:
        # A GitHub/network error degrades the warm-cache to null; the evidence
        # map is still written and the launch proceeds.
        def _boom(*_a, **_k):
            raise RuntimeError("github down")

        ctx, manifest = self._ctx()
        run_dir = tmp_path / "run"
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=SimpleNamespace(get_issue=_boom, get_prs_for_issue=_boom),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=6335,
            board_snapshot=self._board(),
        )
        path = run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME
        assert path.is_file()
        assert json.loads(path.read_text())["github"] is None
        assert manifest["evidence_map"] == str(path)

    def test_write_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The outer best-effort catch: a write failure must neither raise nor
        # record a manifest entry pointing at a file that was not written.
        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(tech_lead_session_policy, "write_evidence_map", _boom)
        ctx, manifest = self._ctx()
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=self._host(),
            ctx=ctx,
            run_dir=tmp_path / "run",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=6335,
            board_snapshot=self._board(),
        )
        assert "evidence_map" not in manifest
