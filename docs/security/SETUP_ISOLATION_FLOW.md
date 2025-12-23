# Setup Flow: Agent Isolation

This is the recommended setup interaction for agent isolation.

## Prompt text (suggested)

**Agent Isolation Mode**
- **Standard (recommended)** — no sudo. Agents run with a scrubbed environment and isolated HOME. Designed to prevent accidental GitHub access.
- **Hardened** — one-time admin setup. Agents run under a dedicated OS user with no credentials, preventing GitHub API access by construction.

Default: **Standard**

## Commands

### Standard
- `issue-orchestrator setup`
- Creates config, validates prerequisites, enables sandbox verification.

### Enable Hardened later
- `issue-orchestrator setup --hardened`
  - requests sudo once
  - provisions sandbox user
  - installs/activates hardened runner mechanism (implementation-dependent)
  - runs `issue-orchestrator verify-agent-sandbox` and prints PASS/FAIL

### Disable Hardened (optional)
- `issue-orchestrator setup --disable-hardened`
  - removes/turns off hardened runner mechanism
  - does **not** need to delete the OS user by default (safer; optional flag to remove)

## UX requirements
- clear explanation of what will change
- explicit "you can enable later" message
- explicit verification step after setup
