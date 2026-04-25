# Agent-Guided Onboarding

This is the same onboarding problem as [Getting Started](getting-started.md), but the AI drives the setup for you instead of making you translate the docs step by step.

## When to use it

Use this path when:
- you already have Codex, Claude Code, or a similar agent open in the repo
- you want a hands-free first run
- you want the agent to detect rough spots and recover instead of stopping at the first confusing prompt

## How to frame it

There are still only two repository-state paths:
- `New repo`
- `Existing repo`

`Agent-guided` is the execution style layered on top of those paths.

## Recommended ask

Tell your AI assistant to:

1. Detect whether this is a `new repo` or `existing repo` onboarding.
2. Run `issue-orchestrator setup` in the target repo.
3. Follow with `issue-orchestrator setup-guardrails` and `issue-orchestrator init`.
4. Review `git status` and publish the generated onboarding files to the worktree seed ref before the first run.
5. Run `issue-orchestrator doctor`.
6. Create or label one trivial issue with a configured agent label.
7. Run `issue-orchestrator start` and confirm the issue reaches `queued` or `running`.
8. If E2E is in scope, enable `e2e:` in config and run a manual E2E start via the control API.

If your AI assistant supports repo-local skills, ask it to use the `onboarding` skill in this repository. That skill is designed to drive exactly this flow and call out known failure modes.

## Practical advice

- Prefer `codex` for the first fully hands-free run when it is available.
- If using `codex`, leave the wizard's Codex model prompt blank so the local Codex CLI can choose its current default.
- When an agent is driving setup, prefer piping scripted stdin into `issue-orchestrator setup` rather than leaving the wizard interactive. Nested interactive prompts are a common place for otherwise-capable agents to stall.
- If you choose `claude-code`, expect a manual workspace-trust prompt the first time Claude opens each new worktree.
- Claude trust is per worktree path. A dedicated worktree base keeps those paths easy to find, but pre-approving the parent worktree directory does not automatically trust future child worktrees.
- Do not leave the default validation command in place if the target repo does not support it.
- Publish `.prompts/` and other generated onboarding files to the worktree seed ref before `start`; by default that means commit and push to your default branch. For local-only evaluation, set `worktrees.seed_ref: HEAD`.
- Treat `doctor` as mandatory before `start`, but only after the onboarding files are on the worktree seed ref or `worktrees.seed_ref: HEAD` is configured.

## E2E note

Programmatic E2E starts require:
- bearer auth from `~/.issue-orchestrator/api-token`
- `repo_root`
- `config_name` such as `default.yaml`

See [E2E Runner](../user/e2e.md) for the exact calls.
