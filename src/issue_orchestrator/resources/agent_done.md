# CRITICAL: You MUST call agent-done before exiting

There is NO other way to complete this session. If you exit without calling `agent-done`, your work is LOST and the session will time out, requiring human intervention.

Read the task-specific prompt file for what to do. Return here for how to signal completion.

---

## Completion Protocol

When your work is done (or you cannot proceed), call `agent-done` with the appropriate status:

### For coding/implementation work

**Completed successfully:**
```bash
agent-done completed \
  --implementation "What you did" \
  --problems "Any issues encountered, or 'None'"
```

**Cannot proceed - external blocker:**
```bash
agent-done blocked \
  --reason "Why you're blocked" \
  --attempted "What you tried" \
  --blocked-by 123 456 \        # Optional: blocking issue numbers
  --when-unblocked "..."        # Optional: hint for resolution
```

**Cannot proceed - gave up:**
```bash
agent-done blocked \
  --reason "Could not complete: <why>" \
  --attempted "Tried X, Y, Z - none worked"
```

**Need human decision:**
```bash
agent-done needs_human \
  --question "What do you need answered?" \
  --context "Background info" \          # Optional
  --options "Option A" "Option B" \      # Optional
  --default "What to do if no response"  # Optional
```

### For code review

**Approved:**
```bash
agent-done approved \
  --summary "Why the code looks good" \
  --risk low|medium|high \
  --checks tests_pass code_quality  # Optional: what you verified
```

**Changes requested:**
```bash
agent-done changes_requested \
  --issues "What needs to be fixed" \
  --risk low|medium|high \
  --checks-needed tests error_handling  # Optional: what's missing
```

### Additional options

All statuses support:
- `--pr-labels label1 label2` - Extra labels to add to the PR
- `--dry-run` - Show what would be written without writing
- `--verbose` - Show detailed output

## What happens after agent-done

1. **Validation runs** (if configured) - tests, linting, type checks
2. **If validation fails**: agent-done exits non-zero. Fix the issues and run agent-done again.
3. **If validation passes**: Completion record is written
4. **Orchestrator takes over**: pushes code, creates PR, posts comments, updates labels

You do NOT push code or touch GitHub directly. The orchestrator handles all external operations.

## If validation keeps failing

If you genuinely cannot fix the validation errors after multiple attempts:

```bash
agent-done blocked \
  --reason "Validation failing: <specific error>" \
  --attempted "Tried to fix by X, Y, Z"
```

This signals you need help without pretending the work is complete.
