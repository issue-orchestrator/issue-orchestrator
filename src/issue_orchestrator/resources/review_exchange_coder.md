# Review Exchange Protocol — Coder

You are participating in an automated coder↔reviewer exchange.

## How to respond

Read the task-specific prompt file for what to fix.

1. **Make the requested changes** in the worktree.
2. **Commit your changes** — the working tree must be clean.
3. **Run `coding-done completed --implementation "..." --problems "..."`** to record your completion and run validation.
4. **Then write exactly one line of JSON** to the file at `$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE`:

**Applied fixes:**
```json
{"response_type":"ok","response_text":"Fixed X, Y, Z as requested."}
```

**Disagree with the feedback:**
```json
{"response_type":"disagree","response_text":"This change is wrong because..."}
```

## CRITICAL rules

- You MUST call `coding-done` first (this creates completion and validation artifacts).
- You MUST also write JSON to `$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE` after coding-done succeeds.
- Do NOT skip, disable, quarantine, or weaken failing tests. For JUnit/Kotlin/Java this includes `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.
- **DO NOT** call `reviewer-done`. That command is for reviewers, not coders.
- Both steps are required. Missing either one will cause a protocol error.
