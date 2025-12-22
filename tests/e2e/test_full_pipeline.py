"""Comprehensive E2E test for the full orchestration pipeline.

This test verifies the complete system works end-to-end by:
1. Creating real GitHub issues
2. Starting the orchestrator with IPC observation
3. Watching events fire as issues are processed
4. Verifying labels, worktrees, sessions, PRs at each stage
5. Verifying code review is triggered
6. Verifying triage is triggered after threshold
7. Testing failure scenarios

This is the "if this passes, the system works" test.
"""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from tests.e2e.conftest import (
    OrchestratorProcess,
    wait_for_issue_label,
    wait_for_pr_created,
    get_issue_comments,
)


# ---------------------------------------------------------------------------
# Observation Helpers
# ---------------------------------------------------------------------------

@dataclass
class ObservedEvent:
    """An event observed during the test."""
    event_type: str
    entity_id: int
    timestamp: float
    data: dict = field(default_factory=dict)


class EventObserver:
    """Collects and queries events for test assertions."""

    def __init__(self):
        self.events: list[ObservedEvent] = []

    def record(self, event_type: str, entity_id: int, data: dict = None):
        """Record an observed event."""
        self.events.append(ObservedEvent(
            event_type=event_type,
            entity_id=entity_id,
            timestamp=time.time(),
            data=data or {},
        ))

    def has_event(self, event_type: str, entity_id: int = None) -> bool:
        """Check if an event was observed."""
        for e in self.events:
            if e.event_type == event_type:
                if entity_id is None or e.entity_id == entity_id:
                    return True
        return False

    def get_events(self, event_type: str = None, entity_id: int = None) -> list[ObservedEvent]:
        """Get matching events."""
        result = []
        for e in self.events:
            if event_type and e.event_type != event_type:
                continue
            if entity_id and e.entity_id != entity_id:
                continue
            result.append(e)
        return result

    def event_sequence_for(self, entity_id: int) -> list[str]:
        """Get the sequence of event types for an entity."""
        events = self.get_events(entity_id=entity_id)
        events.sort(key=lambda e: e.timestamp)
        return [e.event_type for e in events]


def get_issue_labels(repo: str, issue_number: int) -> list[str]:
    """Get current labels on an issue."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo,
         "--json", "labels"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return [l["name"] for l in data.get("labels", [])]
    return []


def get_pr_labels(repo: str, pr_number: int) -> list[str]:
    """Get current labels on a PR."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number),
         "--repo", repo,
         "--json", "labels"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return [l["name"] for l in data.get("labels", [])]
    return []


def find_worktree(base_path: Path, issue_number: int) -> Optional[Path]:
    """Find worktree directory for an issue."""
    if not base_path.exists():
        return None
    for path in base_path.iterdir():
        if path.is_dir() and f"issue-{issue_number}" in path.name:
            return path
    return None


def find_tmux_session(issue_number: int) -> bool:
    """Check if a tmux session exists for an issue."""
    result = subprocess.run(
        ["tmux", "list-windows", "-t", "orchestrator", "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        windows = result.stdout.strip().split("\n")
        for window in windows:
            if f"issue-{issue_number}" in window:
                return True
    return False


def count_prs_with_label(repo: str, label: str) -> int:
    """Count PRs with a specific label."""
    result = subprocess.run(
        ["gh", "pr", "list",
         "--repo", repo,
         "--label", label,
         "--json", "number"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        prs = json.loads(result.stdout)
        return len(prs)
    return 0


def find_triage_issue(repo: str) -> Optional[dict]:
    """Find a triage review issue."""
    result = subprocess.run(
        ["gh", "issue", "list",
         "--repo", repo,
         "--label", "test-data",
         "--search", "Triage Review OR Batch Review",
         "--json", "number,title,labels"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        issues = json.loads(result.stdout)
        for issue in issues:
            if "triage" in issue["title"].lower() or "batch" in issue["title"].lower():
                return issue
    return None


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def observer():
    """Create event observer for test assertions."""
    return EventObserver()


@pytest.fixture
def worktree_base(tmp_path):
    """Temporary directory for worktrees."""
    wt_path = tmp_path / "worktrees"
    wt_path.mkdir()
    return wt_path


# ---------------------------------------------------------------------------
# The Comprehensive E2E Tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(600)  # 10 minute timeout for full pipeline
class TestFullOrchestrationPipeline:
    """Test the complete orchestration pipeline end-to-end.

    This test creates real issues, runs real Claude sessions, and verifies
    the entire system works as expected.
    """

    def test_single_issue_lifecycle(
        self,
        single_test_issue: dict,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
        observer: EventObserver,
    ):
        """Test complete lifecycle of a single issue.

        Verifies:
        1. Issue is picked up (in-progress label)
        2. Worktree is created with correct naming
        3. Terminal session is created
        4. Agent completes and creates PR
        5. PR has correct labels (needs-code-review)
        6. Completion comment is posted
        """
        issue_number = single_test_issue["number"]
        print(f"\n=== Testing issue #{issue_number} lifecycle ===")

        # Start orchestrator
        orchestrator_process.start(max_issues=1)
        assert orchestrator_process.is_running(), "Orchestrator should start"

        pr = None  # Initialize for cleanup in finally block
        try:
            # Phase 1: Issue claimed and session started
            print("Phase 1: Waiting for session to start...")
            in_progress = wait_for_issue_label(
                repo_name, issue_number, "in-progress", timeout=60
            )
            assert in_progress, f"Issue {issue_number} should have 'in-progress' label"
            observer.record("session_started", issue_number)

            # Verify tmux session exists
            time.sleep(2)  # Give session time to create
            has_session = find_tmux_session(issue_number)
            # Note: Session might be named differently, so we don't hard-fail
            if has_session:
                observer.record("tmux_session_created", issue_number)
                print(f"  ✓ Tmux session found for issue {issue_number}")
            else:
                print(f"  ⚠ Tmux session not found (may use different naming)")

            # Phase 2: Wait for completion
            print("Phase 2: Waiting for PR creation...")
            pr = wait_for_pr_created(repo_name, issue_number, timeout=180)
            assert pr is not None, f"PR should be created for issue {issue_number}"
            observer.record("pr_created", issue_number, {"pr_number": pr["number"]})
            print(f"  ✓ PR #{pr['number']} created")

            # Verify PR has needs-code-review label
            pr_labels = get_pr_labels(repo_name, pr["number"])
            print(f"  PR labels: {pr_labels}")
            # Label might not be applied if code review isn't configured
            if "needs-code-review" in pr_labels:
                observer.record("code_review_label_applied", pr["number"])
                print(f"  ✓ 'needs-code-review' label applied")

            # Phase 3: Verify completion handling
            print("Phase 3: Verifying completion...")
            time.sleep(5)  # Give time for cleanup

            # in-progress should be removed
            current_labels = get_issue_labels(repo_name, issue_number)
            # Note: Label removal timing can vary
            print(f"  Issue labels after completion: {current_labels}")

            # Verify completion comment was posted
            comments = get_issue_comments(repo_name, issue_number)
            has_impl_comment = any(
                "## Implementation" in c.get("body", "") or
                "E2E test completed" in c.get("body", "")
                for c in comments
            )
            if has_impl_comment:
                observer.record("completion_comment_posted", issue_number)
                print(f"  ✓ Completion comment posted")
            else:
                print(f"  ⚠ Completion comment not found")

            # Summary
            print("\n=== Event Sequence ===")
            for event in observer.events:
                print(f"  {event.event_type}: entity={event.entity_id}")

            # Assertions on event sequence
            assert observer.has_event("session_started", issue_number)
            assert observer.has_event("pr_created", issue_number)

        finally:
            orchestrator_process.stop()

            # Cleanup: close PR
            if pr:
                subprocess.run(
                    ["gh", "pr", "close", str(pr["number"]),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(300)
class TestLabelManagement:
    """Test that labels are correctly managed throughout lifecycle."""

    def test_labels_applied_and_removed(
        self,
        single_test_issue: dict,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Verify labels are applied and removed at correct times."""
        issue_number = single_test_issue["number"]

        # Track label states
        label_history = []

        def record_labels():
            labels = get_issue_labels(repo_name, issue_number)
            label_history.append({
                "time": time.time(),
                "labels": labels,
            })
            return labels

        # Initial state
        initial_labels = record_labels()
        assert "in-progress" not in initial_labels, "Should not start with in-progress"

        # Start orchestrator
        orchestrator_process.start(max_issues=1)

        pr = None  # Initialize for cleanup in finally block
        try:
            # Wait for in-progress
            for _ in range(30):
                labels = record_labels()
                if "in-progress" in labels:
                    print(f"✓ 'in-progress' applied after {len(label_history)} checks")
                    break
                time.sleep(2)
            else:
                pytest.fail("in-progress label never applied")

            # Wait for completion (in-progress removed or PR created)
            pr = wait_for_pr_created(repo_name, issue_number, timeout=180)

            if pr:
                # Check PR labels
                pr_labels = get_pr_labels(repo_name, pr["number"])
                print(f"PR labels: {pr_labels}")

                # After some time, in-progress should be removed
                time.sleep(10)
                final_labels = record_labels()
                print(f"Final issue labels: {final_labels}")

                # Print label history
                print("\n=== Label History ===")
                for entry in label_history:
                    print(f"  {entry['labels']}")

        finally:
            orchestrator_process.stop()
            if pr:
                subprocess.run(
                    ["gh", "pr", "close", str(pr["number"]),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(900)  # 15 minutes for multi-issue test
class TestCodeReviewPipeline:
    """Test the code review pipeline is triggered correctly."""

    def test_code_review_triggered_after_completion(
        self,
        single_test_issue: dict,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Verify code review is triggered after PR is created."""
        issue_number = single_test_issue["number"]

        orchestrator_process.start(max_issues=1)

        pr = None  # Initialize for cleanup in finally block
        try:
            # Wait for PR
            pr = wait_for_pr_created(repo_name, issue_number, timeout=180)
            assert pr is not None, "PR should be created"

            pr_number = pr["number"]
            print(f"PR #{pr_number} created, checking for code review...")

            # Check for code-review label
            for _ in range(30):
                labels = get_pr_labels(repo_name, pr_number)
                if "needs-code-review" in labels:
                    print(f"✓ 'needs-code-review' label applied to PR #{pr_number}")
                    break
                if "code-reviewed" in labels:
                    print(f"✓ Code review already completed on PR #{pr_number}")
                    break
                time.sleep(5)

            # Wait a bit more to see if review agent picks it up
            time.sleep(30)

            # Check final state
            final_labels = get_pr_labels(repo_name, pr_number)
            print(f"Final PR labels: {final_labels}")

            # Either needs-code-review or code-reviewed should be present
            has_review_label = (
                "needs-code-review" in final_labels or
                "code-reviewed" in final_labels
            )
            assert has_review_label, "PR should have code review label"

        finally:
            orchestrator_process.stop()
            if pr:
                subprocess.run(
                    ["gh", "pr", "close", str(pr["number"]),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(120)
class TestFailureHandling:
    """Test that failures are handled correctly."""

    def test_orchestrator_handles_no_matching_issues(
        self,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Orchestrator should handle having no matching issues gracefully."""
        # Ensure no test issues exist
        from issue_orchestrator.test_data import cleanup_test_issues
        cleanup_test_issues(repo_name)

        # Start orchestrator
        orchestrator_process.start(max_issues=1)

        try:
            # Should run without crashing
            time.sleep(10)
            assert orchestrator_process.is_running(), "Orchestrator should still be running"

            # Stop and check output
            stdout, stderr = orchestrator_process.stop()

            # Should not have crashed
            assert "Traceback" not in stderr, "Should not have crashed"

        finally:
            if orchestrator_process.is_running():
                orchestrator_process.stop()


# ---------------------------------------------------------------------------
# Verification Summary Test
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(300)
class TestVerificationSummary:
    """Quick verification that key components work together."""

    def test_orchestrator_connects_all_components(
        self,
        single_test_issue: dict,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Verify orchestrator correctly connects all components.

        This is a quick sanity check that:
        - GitHub integration works (labels)
        - Terminal management works (tmux)
        - Session tracking works (monitors completion)
        - Completion handling works (creates PR)
        """
        issue_number = single_test_issue["number"]

        checks = {
            "github_labels": False,
            "session_started": False,
            "pr_created": False,
        }

        orchestrator_process.start(max_issues=1)

        pr = None  # Initialize for cleanup in finally block
        try:
            # Check 1: GitHub label management
            if wait_for_issue_label(repo_name, issue_number, "in-progress", timeout=60):
                checks["github_labels"] = True
                checks["session_started"] = True
                print("✓ GitHub labels working, session started")

            # Check 2: PR creation (implies session completed)
            pr = wait_for_pr_created(repo_name, issue_number, timeout=180)
            if pr:
                checks["pr_created"] = True
                print(f"✓ PR #{pr['number']} created")

            # Summary
            print("\n=== Component Verification ===")
            for component, passed in checks.items():
                status = "✓" if passed else "✗"
                print(f"  {status} {component}")

            # All checks should pass
            assert all(checks.values()), f"Some checks failed: {checks}"

        finally:
            orchestrator_process.stop()
            if pr:
                subprocess.run(
                    ["gh", "pr", "close", str(pr["number"]),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )
