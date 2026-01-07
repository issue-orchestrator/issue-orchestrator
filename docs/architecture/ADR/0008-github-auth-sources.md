# ADR 0008: GitHub authentication sources (env-first, optional keychain)

**Status:** Accepted  
**Date:** 2025-12-31

## Context
The project removed the `gh` CLI dependency to gain deterministic timeouts, retries, and avoid subprocess hangs.

## Decision
1) **Primary auth source:** environment variable `ISSUE_ORCH_GITHUB_TOKEN`
2) **Fallback:** `GITHUB_TOKEN`
3) **Optional convenience:** `issue-orchestrator auth store` using OS keychain via `keyring`
4) **Not supported:** gh `hosts.yml` and gh keychain formats

## Consequences
- Simple onboarding
- Cross-platform
- Compatible with agent isolation
