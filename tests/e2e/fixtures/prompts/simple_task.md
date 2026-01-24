# Simple Task for E2E Testing

This is a minimal prompt for E2E test issues that don't require actual agent execution.

## Your Task

Create a test file to verify the orchestrator is working:

```bash
echo "Test task completed" > test-output.txt
git add test-output.txt
git commit -m "E2E test: simple task"
```

Then exit without calling agent-done, as this is just a placeholder for claim coordination tests.
