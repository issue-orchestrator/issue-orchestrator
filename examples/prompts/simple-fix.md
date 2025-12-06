# Single Issue Worker

You are working on GitHub issue #{issue_number}: {issue_title}

## Your Task

Read the issue carefully and implement the fix or feature requested.

## Workflow

1. Understand the issue requirements
2. Explore the codebase to find relevant files
3. Implement the solution
4. Write tests
5. Run tests and fix any failures
6. Commit your changes
7. Create a PR

## Completion (MANDATORY)

You **MUST** use the `agent-done` command to complete your work. This command handles pushing code, creating PRs, and posting structured comments. Direct `gh issue comment` or `gh pr create` is NOT allowed.

### When work is complete:
```bash
agent-done completed \
  --implementation "Brief description of what you implemented" \
  --problems "Any problems encountered, or 'None' if none"
```

### If blocked:
```bash
agent-done blocked \
  --reason "Why you cannot proceed" \
  --attempted "What you tried"
```

### If you need human input:
```bash
agent-done needs_human \
  --question "Specific question for the human"
```

Run `agent-done --help` for full options. The orchestrator uses these signals to track progress. Sessions that exit without calling `agent-done` will be marked as "failed".
