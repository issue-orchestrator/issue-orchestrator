# Security and Isolation Modes

Issue Orchestrator is **control-first**: agents propose work, but publishing and state advancement are gated by the orchestrator and (optionally) OS-level isolation.

This document describes the two supported isolation modes and what each mode guarantees.

---

## Threat model (what we are protecting against)

Agents are extremely capable at editing code, but they are not reliable custodians of process. The main risks we protect against are:

- accidental credential inheritance (tokens, SSH agents, `gh` auth)
- accidental publishing (push/PR/merge)
- bypassing guardrails (skipping tests, skipping review cycles)
- silent workflow drift (labels/PR state not matching expected lifecycle)

We are **not** building a hardened sandbox against a determined local attacker in Standard mode. Hardened mode provides much stronger mechanical isolation.

---

## Mode 1: Standard (default, no sudo)

### What it does
Agents run as the current OS user, but the orchestrator launches agent sessions with a **scrubbed environment** and an **isolated HOME** directory per worktree.

At worktree creation / session start, we run an affirmative sandbox verification step to ensure the agent environment cannot authenticate to GitHub.

### Guarantees (Standard)
- Greatly reduces accidental credential inheritance
- Prevents accidental `gh` authenticated usage (in practice)
- Ensures remote git operations fail fast (no prompts/hangs)
- Keeps agents productive locally (edit code, run tests, `git commit`)

### Non-guarantees (Standard)
Because the agent process runs as the same OS user, a determined agent could still potentially access user-level credential stores depending on OS configuration (e.g., macOS keychain) and available tools.

Standard mode is designed to prevent **accidents**, not to act as an adversarial sandbox.

---

## Mode 2: Hardened (opt-in, one-time sudo)

### What it does
Agents run under a dedicated low-privilege OS user (e.g., `issueorch-agent`) with:

- no GitHub credentials
- no SSH keys
- no `gh` auth state
- no keychain items
- no admin privileges

This closes the remaining gap around GitHub API calls and other authenticated operations by construction.

### Guarantees (Hardened)
- Agents cannot authenticate to GitHub APIs
- Agents cannot push/merge regardless of prompt/tooling
- Credential inheritance becomes structurally impossible (no keychain, no ssh-agent, no tokens)

### Operational note
Hardened mode requires a one-time privileged setup step to create/configure the OS user and any required launch mechanism.

---

## Sandbox verification (both modes)

Every new worktree/session should perform a quick affirmative check. Minimal checks:

- `gh auth status` must fail
- `git push --dry-run` must fail fast (no prompt)
- forbidden env vars are absent (`GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`, etc.)
- (optional) confirm HOME is isolated (Standard) or `whoami` is the sandbox user (Hardened)

If verification fails, the orchestrator should:
- refuse to start the agent
- emit a clear trace event (visible in UI/logs)
- provide remediation guidance

---

## Publishing authority separation

To ensure humans must merge:
- agents do not possess merge-capable GitHub credentials
- orchestrator performs pushing/PR creation under a bot/app identity (optional)
- merges are performed by humans in GitHub UI and enforced via branch protections/CODEOWNERS

---

## Quick start
- Standard: `issue-orchestrator setup` (default)
- Hardened: `issue-orchestrator setup --hardened`
- Verify: `issue-orchestrator verify-agent-sandbox`
