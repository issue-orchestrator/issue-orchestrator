"""E2E tests for issue-orchestrator.

These tests create real GitHub issues and run the full orchestrator lifecycle.
They require:
- GitHub token configured with write access
- `claude` CLI available
- Network access to GitHub

Run with: E2E_TEST_REPO=<owner>/<test-repo> make test-e2e
(collected only with IO_E2E=1, never in an agent session: tests/live_e2e_gate.py)
"""
