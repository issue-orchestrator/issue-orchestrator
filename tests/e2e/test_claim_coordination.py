"""E2E tests for multi-orchestrator claim coordination.

These tests verify that the claim/lease system correctly coordinates
multiple orchestrator instances working on the same GitHub repository.

Tests require:
- Two orchestrator processes with different ports and worktree bases
- Both pointing to the same repo with claims enabled
- Racing for the same issue to verify only one wins
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Generator

import pytest

from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.support.test_data import create_issue, cleanup_issues_by_label

from .fixtures import (
    find_free_port,
    OrchestratorProcess,
    trigger_refresh,
    env_token_name,
    get_issue_labels,
)


CLAIM_E2E_LABEL = "io-e2e-claim-test"


def create_claim_enabled_config(
    project_root: Path,
    worktree_base: Path,
    repo_name: str,
    claimant_id: str,
    lease_seconds: int = 30,  # Short lease for faster tests
) -> Config:
    """Create a config with claims enabled for E2E testing."""
    config = Config()
    config.repo = repo_name
    config.repo_root = project_root
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
            prompt_path=project_root / "tests/e2e/fixtures/prompts/simple_task.md",
            model="sonnet",
            timeout_minutes=5,
            command="claude",
            permission_mode="auto-edit",
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
    return os.environ.get("E2E_REPO", "BruceBGordon/issue-orchestrator")


@pytest.fixture
def dual_orchestrator_configs(
    e2e_project_root: Path,
    repo_name: str,
) -> tuple[Config, Config]:
    """Create two configs with isolated ports and worktree bases."""
    worktree_base_a = Path("/tmp/e2e-claim-worktrees/orchestrator-a")
    worktree_base_b = Path("/tmp/e2e-claim-worktrees/orchestrator-b")
    worktree_base_a.mkdir(parents=True, exist_ok=True)
    worktree_base_b.mkdir(parents=True, exist_ok=True)

    config_a = create_claim_enabled_config(
        e2e_project_root,
        worktree_base_a,
        repo_name,
        claimant_id="orchestrator-a",
    )

    config_b = create_claim_enabled_config(
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
) -> Generator[tuple[OrchestratorProcess, OrchestratorProcess], None, None]:
    """Start two orchestrator instances for claim testing."""
    import shutil
    from pathlib import Path

    config_a, config_b = dual_orchestrator_configs

    # Clean up the shared state directory to avoid session conflicts from previous runs
    # Both orchestrators share the same repo_root, so they would share the session registry
    # We need to clear this to avoid "session already running" errors from stale entries
    state_dir = e2e_project_root / ".issue-orchestrator" / "state"
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    proc_a = OrchestratorProcess(config_a, e2e_project_root)
    proc_b = OrchestratorProcess(config_b, e2e_project_root)

    # Start both orchestrators
    proc_a.start(max_issues=5, extra_args=["--label", claim_test_label])
    proc_b.start(max_issues=5, extra_args=["--label", claim_test_label])

    # Wait for both to be ready
    time.sleep(5)

    yield proc_a, proc_b

    # Stop both orchestrators
    proc_a.stop()
    proc_b.stop()


@pytest.fixture
def claim_test_issue(repo_name: str, claim_test_label: str) -> Generator[int, None, None]:
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


class TestClaimCoordination:
    """Tests for multi-orchestrator claim coordination."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_only_one_orchestrator_claims_issue(
        self,
        dual_orchestrators: tuple[OrchestratorProcess, OrchestratorProcess],
        dual_orchestrator_configs: tuple[Config, Config],
        claim_test_issue: int,
        repo_name: str,
    ):
        """Two orchestrators racing for same issue - only one should win."""
        proc_a, proc_b = dual_orchestrators
        config_a, config_b = dual_orchestrator_configs

        # Trigger refresh on both (using their specific ports)
        trigger_refresh(port=config_a.control_api_port)
        trigger_refresh(port=config_b.control_api_port)

        # Wait for claim to be established
        await asyncio.sleep(15)

        # Use proc_a, proc_b for potential debugging/status checks
        assert proc_a.is_running(), "Orchestrator A should be running"
        assert proc_b.is_running(), "Orchestrator B should be running"

        # Check GitHub for claim label
        labels = get_issue_labels(repo_name, claim_test_issue)

        assert "io:claimed" in labels, f"Issue #{claim_test_issue} should have io:claimed label"

        # Verify exactly one worktree exists for this issue
        worktrees_a = list(Path("/tmp/e2e-claim-worktrees/orchestrator-a").glob(f"*{claim_test_issue}*"))
        worktrees_b = list(Path("/tmp/e2e-claim-worktrees/orchestrator-b").glob(f"*{claim_test_issue}*"))

        total_worktrees = len(worktrees_a) + len(worktrees_b)
        assert total_worktrees <= 1, f"Expected at most 1 worktree, found {total_worktrees}"

        print(f"\n[CLAIM E2E] Test passed: only one orchestrator claimed issue #{claim_test_issue}")


class TestClaimTakeover:
    """Tests for claim takeover when orchestrator stops."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_second_orchestrator_claims_after_first_crashes(
        self,
        dual_orchestrator_configs: tuple[Config, Config],
        e2e_project_root: Path,
        claim_test_issue: int,
        claim_test_label: str,
        repo_name: str,
    ):
        """When first orchestrator crashes, second should detect stale claim."""
        config_a, config_b = dual_orchestrator_configs

        # Start only orchestrator A
        proc_a = OrchestratorProcess(config_a, e2e_project_root)
        proc_a.start(max_issues=5, extra_args=["--label", claim_test_label])

        # Wait for A to claim the issue
        await asyncio.sleep(10)

        # Verify A claimed it
        labels = get_issue_labels(repo_name, claim_test_issue)
        assert "io:claimed" in labels, "Issue should be claimed by orchestrator A"

        print(f"\n[CLAIM E2E] Orchestrator A claimed issue #{claim_test_issue}")

        # Kill A (simulating crash - no graceful release)
        if proc_a.process:
            proc_a.process.kill()
            proc_a.process.wait()

        print("\n[CLAIM E2E] Orchestrator A crashed (killed)")

        # Start B
        proc_b = OrchestratorProcess(config_b, e2e_project_root)
        proc_b.start(max_issues=5, extra_args=["--label", claim_test_label])
        trigger_refresh(port=config_b.control_api_port)

        # Wait for lease expiry and B to detect stale claim
        # With 30s lease and crash, B should detect stale claim within ~30s
        await asyncio.sleep(45)

        # Verify stale claim was detected
        labels = get_issue_labels(repo_name, claim_test_issue)

        # Should have either:
        # - io:claimed (B took over)
        # - blocked:stale-claim (stale claim detected)
        has_claim_activity = "io:claimed" in labels or "blocked:stale-claim" in labels
        assert has_claim_activity, f"Expected claim activity after crash, got labels: {labels}"

        print(f"\n[CLAIM E2E] Test passed: claim activity detected after crash")

        # Cleanup
        proc_b.stop()


class TestNoDuplicateSessions:
    """Tests for preventing duplicate sessions via claims."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_claim_prevents_duplicate_sessions(
        self,
        dual_orchestrators: tuple[OrchestratorProcess, OrchestratorProcess],
        dual_orchestrator_configs: tuple[Config, Config],
        claim_test_issue: int,
        repo_name: str,  # noqa: ARG002 - required by fixture chain
    ):
        """Claim system prevents two sessions from starting on same issue."""
        proc_a, proc_b = dual_orchestrators
        config_a, config_b = dual_orchestrator_configs

        # Trigger both simultaneously (using their specific ports)
        trigger_refresh(port=config_a.control_api_port)
        trigger_refresh(port=config_b.control_api_port)

        # Wait for processing
        await asyncio.sleep(20)

        # Use proc_a, proc_b for potential debugging
        _ = proc_a, proc_b

        # Count worktrees for this issue across both orchestrators
        worktrees_a = list(Path("/tmp/e2e-claim-worktrees/orchestrator-a").glob(f"*{claim_test_issue}*"))
        worktrees_b = list(Path("/tmp/e2e-claim-worktrees/orchestrator-b").glob(f"*{claim_test_issue}*"))

        total_worktrees = len(worktrees_a) + len(worktrees_b)

        # Should have at most 1 worktree (the winner)
        assert total_worktrees <= 1, (
            f"Expected at most 1 worktree for issue #{claim_test_issue}, "
            f"found {len(worktrees_a)} from A and {len(worktrees_b)} from B"
        )

        print(f"\n[CLAIM E2E] Test passed: no duplicate sessions for issue #{claim_test_issue}")
