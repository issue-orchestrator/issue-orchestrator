"""E2E test verifying inflight refresh discovers newly created issues.

This test verifies Phase 1 of the IMPLEMENT_THIS.md spec:
- Create issue while orchestrator runs
- Register inflight ID
- Call refresh with inflight_stable_ids
- Assert issue discovered within bounded time (no arbitrary sleeps)

The key behavior: when GitHub's list API returns 304 (cached),
but we know an issue was just created, we retry without the ETag
cache to ensure we discover it.
"""

import asyncio
import logging
import time

import pytest

from tests.e2e.conftest import (
    e2e_label,
)
from tests.e2e.flows import (
    E2EFlow,
    create_watcher_for_port,
)

logger = logging.getLogger(__name__)


# Max time to wait for issue discovery - with inflight mechanism, should be fast
# but GitHub eventual consistency can take 30-45s in the worst case
DISCOVERY_TIMEOUT = 60


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=100)
async def test_inflight_refresh_discovers_issue(
    e2e_orchestrator,
    e2e_session_config,
) -> None:
    """Test that inflight-registered issues are discovered via refresh.

    This is the core E2E test for the inflight deterministic discovery feature.
    Without this feature, newly created issues might not appear immediately
    due to GitHub's eventual consistency (304 responses from ETag cache).

    The test:
    1. Starts with a running orchestrator
    2. Creates a new issue with external ID prefix
    3. Registers it as inflight
    4. Triggers refresh with inflight IDs
    5. Asserts the issue appears within bounded time (no sleeps)
    """
    # Get config values
    control_api_port = e2e_session_config.control_api_port
    repo = e2e_session_config.repo
    filter_label = e2e_session_config.filtering.label
    agent_label = list(e2e_session_config.agents.keys())[0] if e2e_session_config.agents else "agent:developer"

    # Create watcher
    watcher, stream = await create_watcher_for_port(control_api_port)
    flow = None

    try:
        # Create E2E flow
        flow = E2EFlow(
            repo=repo,
            watcher=watcher,
            filter_label=filter_label,
        )

        # Create unique issue with external ID prefix (M0-XXX format for tests)
        timestamp = int(time.time())
        unique_suffix = str(timestamp % 1000).zfill(3)  # 3-digit suffix
        title = f"[M0-{unique_suffix}] Inflight discovery test"
        labels = [filter_label, agent_label, e2e_label("inflight_refresh_test")]

        logger.info("Creating test issue while orchestrator is running...")

        # Create issue and register as inflight (flow handles registration)
        issue_key, _issue_num = flow.create_issue(title=title, labels=labels)
        logger.info("Created issue: %s (stable_id=%s)", title, issue_key.stable_id())

        # Trigger refresh with inflight IDs - this is the key mechanism
        # The refresh will include the inflight_stable_ids, causing the orchestrator
        # to retry without cache if the issue isn't found.
        # GitHub eventual consistency may require multiple refresh attempts,
        # so we retry every REFRESH_INTERVAL seconds until the issue appears.
        REFRESH_INTERVAL = 15
        stable_id = issue_key.stable_id()
        start = time.monotonic()
        deadline = start + DISCOVERY_TIMEOUT
        last_refresh = 0.0

        while time.monotonic() < deadline:
            # Trigger (or re-trigger) inflight refresh periodically
            now = time.monotonic()
            if now - last_refresh >= REFRESH_INTERVAL:
                from tests.e2e.fixtures.inflight_tracker import trigger_refresh as _trigger
                _trigger(port=control_api_port, inflight_stable_ids={stable_id})
                last_refresh = now
                logger.info("Triggered inflight refresh for %s", stable_id)

            # Check if issue appeared
            issue_view = watcher.view.issues.get(stable_id)
            if issue_view:
                elapsed = time.monotonic() - start
                logger.info("Issue discovered in %.1f seconds", elapsed)
                break

            await asyncio.sleep(1.0)
        else:
            pytest.fail(
                f"Issue {stable_id} not discovered within {DISCOVERY_TIMEOUT}s. "
                "This suggests the inflight refresh mechanism is not working correctly."
            )

        # Verify the issue is in the queue
        assert issue_key.stable_id() in watcher.view.issues, (
            f"Issue {issue_key.stable_id()} not in watcher view after discovery"
        )

    finally:
        # Cleanup
        await watcher.close()
        await stream.close()

        # Close any issues created by the flow
        if flow is not None:
            flow.cleanup_created_issues()
