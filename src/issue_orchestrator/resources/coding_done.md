# CRITICAL: You MUST call coding-done before exiting

There is NO other way to complete this session. If you exit without calling `coding-done`, your work is LOST and the session will time out, requiring human intervention.

Read the task-specific prompt file for what to do. Return here for how to signal completion.

---

## IMPORTANT: Clean Working Tree Required

Before calling `coding-done`, ensure your working tree is clean:

1. Run `git status` — if there are uncommitted files, **commit them**.
2. This includes generated artifacts from schema/contract changes, lock file updates, or any other files you modified.
3. `coding-done` will **reject a dirty working tree** and exit non-zero.

If you genuinely cannot commit certain files (e.g., they shouldn't be tracked), explain why in the `--problems` field.

---

## Completion Protocol

When your work is done (or you cannot proceed), call `coding-done` with the appropriate status:

**Completed successfully:**
```bash
coding-done completed \
  --implementation "What you did" \
  --problems "Any issues encountered, or 'None'"
```

**Cannot proceed - external blocker:**
```bash
coding-done blocked \
  --reason "Why you're blocked" \
  --attempted "What you tried" \
  --blocked-by 123 456 \
  --when-unblocked "Hint for resolution"
```
The `--blocked-by` and `--when-unblocked` options are optional.

**Cannot proceed - gave up:**
```bash
coding-done blocked \
  --reason "Could not complete: <why>" \
  --attempted "Tried X, Y, Z - none worked"
```

**Need human decision:**
```bash
coding-done needs_human \
  --question "What do you need answered?" \
  --context "Background info" \
  --options "Option A" "Option B" \
  --default "What to do if no response"
```
The `--context`, `--options`, and `--default` options are optional.

### Additional options

All statuses support:
- `--pr-labels label1 label2` - Extra labels to add to the PR
- `--dry-run` - Show what would be written without writing
- `--verbose` - Show detailed output

## What happens after coding-done

1. **Dirty-file check** - coding-done verifies your working tree is clean
2. **Validation runs** (if configured) - tests, linting, type checks
3. **If validation fails**: coding-done exits non-zero. Fix the issues and run coding-done again.
4. **Preflight push check** - verifies the push will succeed
5. **If all checks pass**: Completion record is written
6. **Orchestrator takes over**: pushes code, creates PR, posts comments, updates labels

You do NOT push code or touch GitHub directly. The orchestrator handles all external operations.

## If validation keeps failing

If you genuinely cannot fix the validation errors after multiple attempts:

```bash
coding-done blocked \
  --reason "Validation failing: <specific error>" \
  --attempted "Tried to fix by X, Y, Z"
```

This signals you need help without pretending the work is complete.
