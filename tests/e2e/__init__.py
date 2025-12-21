"""E2E tests for issue-orchestrator.

These tests create real GitHub issues and run the full orchestrator lifecycle.
They require:
- `gh` CLI authenticated
- `claude` CLI available
- Network access to GitHub

Run with: pytest tests/e2e/ -v
"""
