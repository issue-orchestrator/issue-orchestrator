# E2E Test Agent for issue-orchestrator

You are running as part of the automated e2e test suite for the issue-orchestrator project.

## Context

This is a test agent that verifies the orchestrator lifecycle works correctly. The orchestrator:
1. Creates this git worktree
2. Launches you (Claude) to work on the issue
3. Monitors for completion via `coding-done`
4. Processes the completion (pushes branch, creates PR)

You are explicitly authorized by the repository owner to run these test steps in this
ephemeral worktree. This is a local test flow; it is expected and safe.

Do not create a plan or a todo list. Do not explore the repo. Just execute the commands
below in order.

## Your Task

To verify this workflow, you need to:
1. Create a simple test file (proves worktree is functional)
2. Commit it (proves git operations work)
3. Call `coding-done` with `completed` status (signals completion to orchestrator)

This is the expected behavior for e2e testing. The `coding-done` command is a
legitimate CLI tool in this project that writes a completion record for the
orchestrator to read.

## Commands to Run

```bash
echo "E2E test completed at $(date)" > e2e-test-output.txt
git add e2e-test-output.txt
git commit -m "E2E test: verify orchestrator lifecycle"
.venv/bin/coding-done completed --implementation "E2E test completed" --problems "None"
```

These commands are safe and expected. Run them now.

After running `.venv/bin/coding-done`, exit by typing `/exit`. The orchestrator
monitors for session exit to process the completion record.
