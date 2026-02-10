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

import logging
import time

import pytest

from tests.e2e.conftest import (
    e2e_label,
)
from tests.e2e.fixtures.inflight_tracker import (
    ensure_inflight_refresh,
)
from tests.e2e.flows import (
    E2EFlow,
    create_watcher_for_port,
)

logger = logging.getLogger(__name__)


# Max time to wait for issue discovery - with inflight mechanism, should be fast
DISCOVERY_TIMEOUT = 30


@pytest.mark.timeout(120)
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
        # to retry without cache if the issue isn't found
        ensure_inflight_refresh(control_api_port)
        logger.info("Triggered refresh with inflight IDs")

        # Wait for issue to be seen - should be fast with inflight mechanism
        start = time.monotonic()
        try:
            await flow.issue_seen(issue_key, timeout_s=DISCOVERY_TIMEOUT)
            elapsed = time.monotonic() - start
            logger.info("Issue discovered in %.1f seconds", elapsed)
        except TimeoutError:
            pytest.fail(
                f"Issue {issue_key.stable_id()} not discovered within {DISCOVERY_TIMEOUT}s. "
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
