# CRITICAL: You MUST call reviewer-done before exiting

There is NO other way to complete this session. If you exit without calling `reviewer-done`, your work is LOST and the session will time out, requiring human intervention.

Read the task-specific prompt file for what to do. Return here for how to signal completion.

---

## IMPORTANT: Do Not Accept Test Skips

Treat newly added test skips or weakened assertions as blocking unless the task explicitly required them. Examples include `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.

---

## Completion Protocol

When your review is done, call `reviewer-done` with the appropriate verdict:

**Approved:**
```bash
reviewer-done approved \
  --summary "Why the code looks good" \
  --risk low \
  --checks tests_pass code_quality
```
The `--checks` option is optional. Risk must be `low`, `medium`, or `high`.

**Changes requested:**
```bash
reviewer-done changes_requested \
  --issues "What needs to be fixed" \
  --risk medium \
  --checks-needed tests error_handling
```
The `--checks-needed` option is optional.

### Additional options

Both statuses support:
- `--pr-labels label1 label2` - Extra labels to add to the PR
- `--dry-run` - Show what would be written without writing
- `--verbose` - Show detailed output

## What happens after reviewer-done

1. **Completion record is written** with your verdict
2. **Orchestrator takes over**: updates labels, posts review comments, triggers rework if needed

You do NOT modify code, push, or touch GitHub directly. The orchestrator handles all external operations.
