---
name: onboarding
description: Guide a user or AI agent through first-time issue-orchestrator onboarding for a target repository. Use when setting up a brand-new repo, onboarding an existing repo, validating the first issue flow, or enabling the E2E runner on a real repo.
---

# Onboarding

Use this skill when the job is "get issue-orchestrator working in this target repo" rather than "change orchestrator code."

## Pick the path

- `New repo`: no existing issue-orchestrator config, labels, or agent prompts to preserve.
- `Existing repo`: there may be real issues, labels, prompts, or config that should survive onboarding.
- `Agent-guided`: same repo-state decision as above, but the AI drives the setup and validation hands-free.

## Core workflow

1. Confirm you are in the **target repo**, not the `issue-orchestrator` checkout.
2. Verify prerequisites:
   - `issue-orchestrator` CLI is available from the shared dev env.
   - GitHub auth works for the target repo.
   - At least one supported AI CLI is installed.
3. Choose `issue-orchestrator setup` path:
   - `New project - set up from scratch` for new repos.
   - `Existing project - I have labels/issues already` for repos with existing state.
4. Always follow setup with:
   - `issue-orchestrator setup-guardrails`
   - `issue-orchestrator init`
5. Publish the generated onboarding files to the worktree seed ref before the first issue run:
   - at minimum, the configured prompt files and `.issue-orchestrator/config`
   - by default this means commit and push them to your default branch
   - for local-only evaluation, set `worktrees.seed_ref: HEAD`
6. Run `issue-orchestrator doctor`
7. Validate a real first issue:
   - create or label one trivial GitHub issue with a configured agent label
   - run `issue-orchestrator start`
   - confirm the issue reaches `queued` or `running`
8. If E2E is in scope:
   - add an `e2e:` section to config
   - run the manual E2E API start call with bearer auth and `config_name`
   - confirm the run reaches `passed` or produce the exact failing note

## Defaults and guardrails

- Prefer a fixed `ui.web_port` like `8080` for first-run clarity.
- Prefer a validation command that actually exists in the target repo. Do not leave `make test` in place if the repo does not have it.
- Existing-project onboarding is incomplete until at least one agent is configured.
- Treat `doctor` as mandatory before `start`, but only after the onboarding files are on the worktree seed ref or `worktrees.seed_ref: HEAD` is configured.

## GitHub auth

- A token merely existing is not enough; it must access the target repo.
- If setup fails while creating labels, tell the user to rerun `issue-orchestrator doctor`.
- `issue-orchestrator auth store` is the fallback when a local token needs to live in keychain storage.

## AI CLI guidance

- Prefer `codex` for the smoothest hands-free first run when it is available.
- If the user picks `codex`, prefer leaving the wizard's model prompt blank so Codex uses its current CLI default.
- For agent-guided runs, prefer scripted stdin for `issue-orchestrator setup` instead of leaving the wizard interactive. Nested interactive CLIs are a common failure mode for both Codex and Claude-style agents.
- If the repo is using `claude-code`, warn that Claude may pause on workspace trust prompts in fresh worktrees.
- If Claude is the only option, call out that the first runtime validation should explicitly check for a stuck trust prompt in the session recording.

## E2E specifics

- The control API requires:
  - bearer auth from `~/.issue-orchestrator/api-token`
  - `repo_root`
  - `config_name` such as `default.yaml`
- Repos without `uv.lock` are valid.
- Repos whose synced env lacks `pytest` are still valid; the E2E worktree bootstraps a fallback `pytest>=8.0`.

## When to stop and report

Stop and surface the exact blocker when:
- GitHub auth cannot access the repo
- the selected AI CLI is missing
- `doctor` still returns errors after setup
- the first issue never leaves `queued`
- E2E cannot start or finishes with `status=error`

Report findings in three buckets:
- `setup/docs drift`
- `runtime UX`
- `agent-guided opportunities`
