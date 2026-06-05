"""E2E tests for multi-orchestrator claim coordination.

These tests verify that the claim/lease system correctly coordinates
multiple orchestrator instances working on the same GitHub repository.

Tests require:
- Two orchestrator processes with different ports and worktree bases
- Two isolated local repo roots/state directories
- Both pointing to the same repo with claims enabled
- Racing for the same issue to verify only one wins

This is an automated distributed-claim test harness, not a supported
human-facing workflow for running two local orchestrators on one repo.
"""

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import httpx
import pytest

from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.support.test_data import (
    create_issue,
    cleanup_issues_by_label,
)

from .fixtures import (
    find_free_port,
    OrchestratorProcess,
    trigger_refresh,
    control_api_headers,
    env_token_name,
    get_issue_labels,
    get_test_repo,
    keep_artifacts,
)


CLAIM_E2E_LABEL = "io-e2e-claim-test"


@dataclass(frozen=True)
class ControlApiSessionEntry:
    """Active session reported by one orchestrator control API."""

    claimant_id: str
    session_name: str
    issue_number: int
    api_port: int

    def debug_summary(self) -> dict[str, str | int]:
        return {
            "claimant_id": self.claimant_id,
            "session_name": self.session_name,
            "issue_number": self.issue_number,
            "api_port": self.api_port,
        }


def _clone_repo_for_claim_orchestrator(source_root: Path, target_root: Path) -> Path:
    """Create an isolated local repo root for one claim-test orchestrator.

    The clone gives each process its own `.issue-orchestrator/state` directory.
    The process still runs the source tree under test via OrchestratorProcess's
    source_root so uncommitted test harness changes do not need to be copied.
    """
    if target_root.exists():
        shutil.rmtree(target_root, ignore_errors=True)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-hardlinks",
            str(source_root),
            str(target_root),
        ],
        check=True,
        cwd=source_root,
    )
    return target_root


def _active_control_api_sessions(
    processes: tuple[OrchestratorProcess, ...],
    issue_number: int,
) -> list[ControlApiSessionEntry]:
    """Read matching active sessions from each orchestrator's control API."""
    entries: list[ControlApiSessionEntry] = []
    for proc in processes:
        if not proc.is_running():
            continue
        api_port = proc.config.control_api_port
        response = httpx.get(
            f"http://127.0.0.1:{api_port}/api/status",
            timeout=5,
            headers=control_api_headers(),
        )
        response.raise_for_status()
        claimant_id = proc.config.claims.claimant_id or str(proc.project_root)
        for session in response.json().get("sessions", []):
            if session.get("issue_number") != issue_number:
                continue
            entries.append(
                ControlApiSessionEntry(
                    claimant_id=claimant_id,
                    session_name=str(session.get("session_name") or ""),
                    issue_number=int(session["issue_number"]),
                    api_port=api_port,
                )
            )
    return entries


def _assert_at_most_one_active_issue_session(
    processes: tuple[OrchestratorProcess, ...],
    issue_number: int,
) -> list[ControlApiSessionEntry]:
    """Assert duplicate sessions are absent across independent orchestrator APIs."""
    active_entries = _active_control_api_sessions(processes, issue_number)
    assert len(active_entries) <= 1, (
        f"Expected at most 1 active issue session across isolated orchestrator APIs, "
        f"found {len(active_entries)}: {[entry.debug_summary() for entry in active_entries]}"
    )
    return active_entries


def create_claim_enabled_config(
    repo_root: Path,
    source_root: Path,
    worktree_base: Path,
    repo_name: str,
    claimant_id: str,
    lease_seconds: int = 30,  # Short lease for faster tests
) -> Config:
    """Create a config with claims enabled for E2E testing."""
    config = Config()
    config.repo = repo_name
    config.repo_root = repo_root
    config.worktree_base = worktree_base
    config.github_token_env = env_token_name()
    config.control_api_port = find_free_port()
    config.web_port = find_free_port()
    config.queue_refresh_seconds = 5  # Fast refresh for tests
    config.max_concurrent_sessions = 2
    config.session_timeout_minutes = 5
    config.terminal_adapter = "subprocess"
    config.ui_mode = "web"

    # Enable claims with short lease times for testing
    config.claims.enabled = True
    config.claims.claimant_id = claimant_id
    config.claims.lease_seconds = lease_seconds
    config.claims.renew_before_expiry_seconds = lease_seconds // 3  # ~10s

    # Use test agent
    from issue_orchestrator.infra.config import AgentConfig

    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=source_root / "tests/e2e/fixtures/prompts/simple_task.md",
            model="sonnet",
            timeout_minutes=5,
            command="claude",
            ai_system="claude-code",
            provider_args={"permission_mode": "auto-edit"},
        ),
    }

    return config


@pytest.fixture
def claim_test_label() -> str:
    """Unique label for claim coordination tests."""
    return CLAIM_E2E_LABEL


@pytest.fixture
def e2e_project_root() -> Path:
    """Project root for E2E tests."""
    # Find project root by looking for pyproject.toml
    current = Path(__file__).parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    raise RuntimeError("Could not find project root")


@pytest.fixture
def repo_name() -> str:
    """Repository name for E2E tests."""
    return os.environ.get("E2E_REPO", get_test_repo())


@pytest.fixture
def dual_orchestrator_configs(
    e2e_project_root: Path,
    tmp_path: Path,
    repo_name: str,
) -> tuple[Config, Config]:
    """Create two configs with isolated repo roots, ports, and worktree bases."""
    repo_root_a = _clone_repo_for_claim_orchestrator(
        e2e_project_root,
        tmp_path / "repos" / "orchestrator-a",
    )
    repo_root_b = _clone_repo_for_claim_orchestrator(
        e2e_project_root,
        tmp_path / "repos" / "orchestrator-b",
    )
    worktree_base_a = tmp_path / "worktrees" / "orchestrator-a"
    worktree_base_b = tmp_path / "worktrees" / "orchestrator-b"
    worktree_base_a.mkdir(parents=True, exist_ok=True)
    worktree_base_b.mkdir(parents=True, exist_ok=True)

    config_a = create_claim_enabled_config(
        repo_root_a,
        e2e_project_root,
        worktree_base_a,
        repo_name,
        claimant_id="orchestrator-a",
    )

    config_b = create_claim_enabled_config(
        repo_root_b,
        e2e_project_root,
        worktree_base_b,
        repo_name,
        claimant_id="orchestrator-b",
    )

    return config_a, config_b


@pytest.fixture
def dual_orchestrators(
    dual_orchestrator_configs: tuple[Config, Config],
    e2e_project_root: Path,
    claim_test_label: str,
    claim_test_issue: int,  # Must resolve before orchestrators start (cleans stale issues)
) -> Generator[tuple[OrchestratorProcess, OrchestratorProcess], None, None]:
    """Start two claim-test orchestrators with independent local state."""
    config_a, config_b = dual_orchestrator_configs

    assert config_a.repo_root != config_b.repo_root
    assert config_a.worktree_base != config_b.worktree_base

    proc_a = OrchestratorProcess(
        config_a, config_a.repo_root, source_root=e2e_project_root
    )
    proc_b = OrchestratorProcess(
        config_b, config_b.repo_root, source_root=e2e_project_root
    )

    # Start both orchestrators
    proc_a.start(max_issues=5, extra_args=["--label", claim_test_label])
    proc_b.start(max_issues=5, extra_args=["--label", claim_test_label])

    proc_a.wait_until_ready()
    proc_b.wait_until_ready()

    yield proc_a, proc_b

    # Stop both orchestrators
    proc_a.stop()
    proc_b.stop()

    if not keep_artifacts():
        shutil.rmtree(config_a.repo_root, ignore_errors=True)
        shutil.rmtree(config_b.repo_root, ignore_errors=True)


@pytest.fixture
def claim_test_issue(
    repo_name: str, claim_test_label: str
) -> Generator[int, None, None]:
    """Create a test issue for claim coordination tests."""
    # Clean up any existing test issues
    cleanup_issues_by_label(repo_name, claim_test_label)

    # Create a new test issue
    issue_number = create_issue(
        repo_name,
        "[E2E-CLAIM] Coordination test issue",
        ["agent:e2e-test", claim_test_label],
        body="Test issue for multi-orchestrator claim coordination.",
    )

    print(f"\n[CLAIM E2E] Created test issue #{issue_number}")
    yield issue_number

    # Cleanup
    print(f"\n[CLAIM E2E] Cleaning up test issue #{issue_number}")
    cleanup_issues_by_label(repo_name, claim_test_label)


@pytest.mark.e2e
@pytest.mark.live
class TestClaimCoordination:
    """Tests for multi-orchestrator claim coordination."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    @pytest.mark.gh_activity_limit(
        test_gh_activity_limit=200, system_gh_activity_limit=100
    )
    async def test_only_one_orchestrator_claims_issue(
        self,
        dual_orchestrators: tuple[OrchestratorProcess, OrchestratorProcess],
        dual_orchestrator_configs: tuple[Config, Config],
        claim_test_issue: int,
        repo_name: str,
    ):
        """Two orchestrators racing for same issue - only one should win.

        Each orchestrator has its own local repo root and state directory.
        This test aggregates each process's public control API session list so
        the assertion matches the multi-machine claim contract.
        """
        proc_a, proc_b = dual_orchestrators
        config_a, config_b = dual_orchestrator_configs

        # Trigger refresh on both (using their specific ports)
        trigger_refresh(port=config_a.control_api_port)
        trigger_refresh(port=config_b.control_api_port)

        # Poll until at least one orchestrator claims/starts the issue.
        claim_labels = {"io:claimed", "in-progress", "blocked:claim-lost"}
        claim_deadline = time.time() + 60
        labels: list[str] = []
        active_entries: list[ControlApiSessionEntry] = []
        while time.time() < claim_deadline:
            await asyncio.sleep(5)
            labels = get_issue_labels(repo_name, claim_test_issue)
            active_entries = _active_control_api_sessions(
                (proc_a, proc_b),
                claim_test_issue,
            )
            if claim_labels & set(labels) or active_entries:
                break

        # Both orchestrators should still be running (no crashes)
        assert proc_a.is_running(), "Orchestrator A should be running"
        assert proc_b.is_running(), "Orchestrator B should be running"

        found = claim_labels & set(labels)
        assert found or active_entries, (
            f"Issue #{claim_test_issue} should have at least one claim/session label "
            f"or active control API session ({claim_labels}), got labels={labels}, "
            f"active_entries={[entry.debug_summary() for entry in active_entries]}"
        )

        active_entries = _assert_at_most_one_active_issue_session(
            (proc_a, proc_b),
            claim_test_issue,
        )

        print(
            f"\n[CLAIM E2E] Test passed: claim coordination worked for issue #{claim_test_issue}"
        )
        print(f"  Labels: {labels}")
        print(
            f"  Active session entries: {[entry.debug_summary() for entry in active_entries]}"
        )


@pytest.mark.e2e
@pytest.mark.live
class TestClaimTakeover:
    """Tests for stale claim handling when an orchestrator stops."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    @pytest.mark.gh_activity_limit(
        test_gh_activity_limit=300, system_gh_activity_limit=150
    )
    async def test_second_orchestrator_detects_stale_claim_after_first_crashes(
        self,
        dual_orchestrator_configs: tuple[Config, Config],
        e2e_project_root: Path,
        claim_test_issue: int,
        claim_test_label: str,
        repo_name: str,
    ):
        """When first orchestrator crashes, second should detect the stale claim."""
        config_a, config_b = dual_orchestrator_configs

        # Start only orchestrator A
        proc_a = OrchestratorProcess(
            config_a, config_a.repo_root, source_root=e2e_project_root
        )
        proc_a.start(max_issues=5, extra_args=["--label", claim_test_label])

        # Poll until A claims the issue — startup scan + claim convergence + session
        # launch can take 15-30s depending on GitHub API latency.
        claim_deadline = time.time() + 60
        labels: list[str] = []
        while time.time() < claim_deadline:
            await asyncio.sleep(5)
            labels = get_issue_labels(repo_name, claim_test_issue)
            if "io:claimed" in labels or "in-progress" in labels:
                break
        assert "io:claimed" in labels or "in-progress" in labels, (
            f"Issue should be claimed/in-progress by orchestrator A, got: {labels}"
        )

        print(f"\n[CLAIM E2E] Orchestrator A claimed issue #{claim_test_issue}")

        # Kill A (simulating crash - no graceful release)
        if proc_a.process:
            proc_a.process.kill()
            proc_a.process.wait()

        print("\n[CLAIM E2E] Orchestrator A crashed (killed)")

        # Start B
        proc_b = OrchestratorProcess(
            config_b, config_b.repo_root, source_root=e2e_project_root
        )
        proc_b.start(max_issues=5, extra_args=["--label", claim_test_label])
        trigger_refresh(port=config_b.control_api_port)

        # Wait for A's lease to expire, then explicitly refresh B. The old
        # io:claimed label is not evidence of B doing anything; under the
        # current policy B must mark the stale claim rather than start work.
        await asyncio.sleep(config_b.claims.lease_seconds + 2)
        trigger_refresh(port=config_b.control_api_port)

        takeover_deadline = time.time() + 60
        labels = []
        b_active_entries: list[ControlApiSessionEntry] = []
        while time.time() < takeover_deadline:
            await asyncio.sleep(5)
            labels = get_issue_labels(repo_name, claim_test_issue)
            b_active_entries = _active_control_api_sessions((proc_b,), claim_test_issue)
            if "blocked:stale-claim" in labels:
                break

        assert "blocked:stale-claim" in labels, (
            f"Expected B to mark stale claim after crash; labels={labels}, "
            f"b_active_entries={[entry.debug_summary() for entry in b_active_entries]}"
        )
        assert not b_active_entries, (
            "Current stale-claim policy should block rather than start a B session; "
            f"b_active_entries={[entry.debug_summary() for entry in b_active_entries]}"
        )
        _assert_at_most_one_active_issue_session((proc_a, proc_b), claim_test_issue)

        print("\n[CLAIM E2E] Test passed: stale claim handled after crash")

        # Cleanup
        proc_b.stop()
        if not keep_artifacts():
            shutil.rmtree(config_a.repo_root, ignore_errors=True)
            shutil.rmtree(config_b.repo_root, ignore_errors=True)


@pytest.mark.e2e
@pytest.mark.live
class TestNoDuplicateSessions:
    """Tests for preventing duplicate sessions via claims."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    @pytest.mark.gh_activity_limit(
        test_gh_activity_limit=200, system_gh_activity_limit=100
    )
    async def test_claim_prevents_duplicate_sessions(
        self,
        dual_orchestrators: tuple[OrchestratorProcess, OrchestratorProcess],
        dual_orchestrator_configs: tuple[Config, Config],
        claim_test_issue: int,
        repo_name: str,
    ):
        """Claim system prevents two sessions from starting on same issue."""
        proc_a, proc_b = dual_orchestrators
        config_a, config_b = dual_orchestrator_configs

        # Trigger both simultaneously (using their specific ports)
        trigger_refresh(port=config_a.control_api_port)
        trigger_refresh(port=config_b.control_api_port)

        # Poll until at least one orchestrator processes the issue (label change
        # or per-process control API session entry), then verify no duplicates.
        claim_labels = {"io:claimed", "in-progress", "blocked:claim-lost"}
        poll_deadline = time.time() + 60
        labels: list[str] = []
        active_entries: list[ControlApiSessionEntry] = []
        while time.time() < poll_deadline:
            await asyncio.sleep(5)
            labels = get_issue_labels(repo_name, claim_test_issue)
            active_entries = _active_control_api_sessions(
                (proc_a, proc_b), claim_test_issue
            )
            if claim_labels & set(labels) or active_entries:
                break

        assert claim_labels & set(labels) or active_entries, (
            f"Issue #{claim_test_issue} should have claim/session activity; "
            f"labels={labels}, active_entries={[entry.debug_summary() for entry in active_entries]}"
        )
        active_entries = _assert_at_most_one_active_issue_session(
            (proc_a, proc_b),
            claim_test_issue,
        )

        print(
            f"\n[CLAIM E2E] Test passed: no duplicate sessions for issue #{claim_test_issue}"
        )
        print(
            f"  Active session entries: {[entry.debug_summary() for entry in active_entries]}"
        )
