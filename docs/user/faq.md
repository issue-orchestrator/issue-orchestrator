# FAQ

## Getting Started

**Q1: Where does the config live, and how do I pick a named config?**
A: By default, config lives at `.issue-orchestrator/config/default.yaml`. You can also create a named config like `.issue-orchestrator/config/main.yaml` and select it in the Control Center: pick the repo, then use the `Config` dropdown (it appears when multiple configs exist) and optionally set a default. In VS Code, set `issueOrchestrator.configPath` in settings.

**Q2: What is the minimum config to run anything?**
A: You need at least one agent definition and a validation command. See the minimal example in [Configuration](configuration.md) under "TL;DR - Starter Config to Get Running."

**Q3: How does the repo root and repo name get set?**
A: The orchestrator auto-detects the repository from the git checkout. Override it only if detection isn't correct, using `repo.root` for the path and `repo.name` for the GitHub repo slug.

**Q4: Where do worktrees go by default, and can I move them?**
A: Worktrees default to `../` via `worktrees.base`. Set `worktrees.base` to point somewhere else if you want to keep them in a dedicated directory.

**Q5: What happens if I change the worktree location after I've been running for a while?**
A: Existing worktrees stay where they were created. Changing `worktrees.base` only affects new worktrees. If you want everything under the new base, clean up old worktrees and let the orchestrator recreate them (or move them manually and make sure any in-flight sessions are stopped first).

**Q6: How do I limit which issues get picked up while I'm learning?**
A: Use `filtering.label`, `filtering.milestone`, `filtering.milestones`, or `filtering.issue` to scope the queue. You can also exclude labels with `filtering.exclude_labels`. Examples:

```yaml
# Only issues labeled "bot-ready"
filtering:
  label: "bot-ready"
```

```yaml
# Only issues in M1 or M2, but never "test-data"
filtering:
  milestones: ["M1", "M2"]
  exclude_labels: ["test-data"]
```

```yaml
# Only a single issue (by number)
filtering:
  issue: 123
```

**Q7: What are the guardrails, and which ones do I need to set up?**
A: The guardrails are the repo's safety hooks that prevent unsafe operations (for example, bypassing validation or pushing without completing). Set up the worktree hooks once per machine, then keep `security.enforce_hooks` enabled. For details, see [Guardrails & Safety Model](../../docs/design/guardrails.md) and [Hook Enforcement](../architecture/hooks.md).

## Everyday Configuration

**Q8: How do I cap concurrency or change timeouts?**
A: Use `execution.concurrency.max_concurrent_sessions` and `execution.concurrency.session_timeout_minutes`.

**Q9: How do I enable code review and set a default reviewer?**
A: Set `review.enabled: true`, then `review.default` to the reviewer agent label (for example, `agent:reviewer`). Make sure that agent is defined under `agents`.

**Q10: Can I reference environment variables in config?**
A: Yes. Any string can use `${VAR}` substitution. If the variable is missing, config loading fails with a clear error pointing to the field.

**Q11: Why does validation fail because the worktree is "dirty," and can I relax it?**
A: The guard prevents a mismatch between what you validated and what you push. Adjust `validation.pre_push_dirty_check` to `unstaged` or `off` if you intentionally want to allow that risk.

## Using the System

**Q12: Can I access this from VS Code (or derivatives like Cursor)?**
A: Yes. See [VS Code Integration](vscode.md). VS Code derivatives that support extensions generally work the same way, as long as they can run the extension and have the `issue-orchestrator-mcp` entrypoint on your PATH.

**Q13: An issue failed. Now what?**
A: Start with the Control Center: open the issue, check the last agent message, and look for the `blocked`/`validation-failed`/`needs-human` labels. Use the Doctor panel to validate config and environment. If the failure is due to validation, re-run the validation command in the worktree, fix the errors, and re-run `agent-done`. For deeper troubleshooting, see [Troubleshooting](../development/TROUBLESHOOTING.md).

**Q14: My issue ran but was blocked. Now what?**
A: Read the agent's last comment and the `blocked` label reason. Provide the missing input, then re-queue the issue by removing the `blocked` label and re-applying the agent label.

**Q15: Why isn't my issue showing up?**
A: Check the Control Center's **Excluded** tab first. Issues can be excluded by filters (`filtering.label`, `filtering.milestone(s)`, `filtering.issue`, `filtering.exclude_labels`), missing agent labels, or missing milestones. If it's excluded, the UI explains why.

## Advanced / Later-Stage Usage

**Q16: How do I control triage issue labels and priority?**
A: Use `triage.explicit_labels` to always apply labels, `triage.inherit_labels` to copy labels from linked issues/PRs, and `triage.priority` to add a specific priority label (for example, `priority:high`).

**Q17: How is the triage milestone chosen, and can I override it?**
A: `triage.milestone_strategy.inherit_from_issues` pulls from linked issues by default. Set `triage.milestone_strategy.explicit` to force a specific milestone.

**Q18: I run multiple orchestrators. How do I avoid collisions?**
A: Think in three cases:

**Case A: Two repos, two orchestrators (same machine).**
Each repo has its own config. No collision because each orchestrator is bound to a different repo.

**Case B: Multiple machines for the same repo.**
Only do this with the claims system enabled. Each machine must set a unique `claims.claimant_id` (for example via `${ORCHESTRATOR_ID}`). Claims add labels like `io:claimed` and enforce lease rules so only one machine works an issue at a time.

**Case C: Multiple orchestrators for the same repo on one machine (advanced).**
This is mainly for development/testing. Use `ui.instances` with `claims.enabled: true` so each instance has a unique claimant ID. Don't do it without claims.

**Q19: How do I tune the review + rework loop?**
A: Use `review.max_rework_cycles` to cap rework (default: 10). When review is enabled, a coder agent produces changes, then a reviewer agent evaluates them. If the reviewer requests changes, the orchestrator opens a rework cycle. This repeats until the reviewer approves or the max is reached, at which point the issue is escalated.

**Q20: How do I manage issue dependencies, and what restrictions apply?**
A: Put dependency lines in the issue body using `Depends-on:`. An issue is runnable only when **all** dependencies are closed. Restrictions:

- Dependencies must be in the **same milestone**, or in the **foundation milestone** (configured via `milestones.foundation`, default `M0`).
- Missing or cross-milestone dependencies block the issue until fixed.

Examples:

```text
Depends-on: #123                    # Same-milestone dependency
Depends-on: org/other-repo#456      # Cross-repo dependency
Depends-on: #12                     # Foundation dependency (issue in M0)
```

If a dependency violates the milestone rule, the issue is marked blocked with a dependency reason in the UI.
