"""Test data management for development and testing."""

import json
import subprocess
from typing import Optional


def cleanup_test_issues(repo: str) -> int:
    """Close all issues with 'test-data' label.

    Returns:
        Number of issues closed
    """
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--label", "test-data",
         "--state", "open", "--json", "number"],
        capture_output=True, text=True
    )

    count = 0
    if result.returncode == 0:
        issues = json.loads(result.stdout)
        for issue in issues:
            subprocess.run(
                ["gh", "issue", "close", str(issue["number"]), "--repo", repo,
                 "--comment", "Closed by test cleanup."],
                capture_output=True
            )
            count += 1

    return count


def create_test_issues(repo: str, agent_labels: Optional[list[str]] = None) -> list[str]:
    """Create test issues for testing.

    Args:
        repo: GitHub repo in owner/repo format
        agent_labels: List of agent labels to use (e.g., ["agent:backend", "agent:frontend"])
                     Defaults to ["agent:backend", "agent:frontend", "agent:mobile"]

    Returns:
        List of created issue URLs
    """
    if agent_labels is None:
        agent_labels = ["agent:backend", "agent:frontend", "agent:mobile"]

    # Create test-data label if missing
    subprocess.run(
        ["gh", "label", "create", "test-data", "--repo", repo, "--force",
         "--description", "Test data for integration tests"],
        capture_output=True
    )

    # Create 5 test issues
    test_issues = [
        ("[TEST] Simple backend task", agent_labels[0] if len(agent_labels) > 0 else "agent:backend", "priority:high"),
        ("[TEST] Frontend feature", agent_labels[1] if len(agent_labels) > 1 else agent_labels[0], "priority:medium"),
        ("[TEST] Mobile bug fix", agent_labels[2] if len(agent_labels) > 2 else agent_labels[0], "priority:low"),
        ("[TEST] Task that will block", agent_labels[0] if len(agent_labels) > 0 else "agent:backend", None),
        ("[TEST] Task with dependency", agent_labels[0] if len(agent_labels) > 0 else "agent:backend", None),
    ]

    created_urls = []

    for title, agent_label, priority_label in test_issues:
        # Create labels if needed
        labels_to_create = [agent_label]
        if priority_label:
            labels_to_create.append(priority_label)
        for label in labels_to_create:
            subprocess.run(
                ["gh", "label", "create", label, "--repo", repo, "--force"],
                capture_output=True
            )

        # Build issue create command
        cmd = ["gh", "issue", "create", "--repo", repo, "--title", title,
               "--body", f"Test issue for orchestrator.\n\nExpected: Agent completes.",
               "--label", "test-data", "--label", agent_label]
        if priority_label:
            cmd.extend(["--label", priority_label])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            created_urls.append(issue_url)

    return created_urls
