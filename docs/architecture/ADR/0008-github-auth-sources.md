# ADR 0008: GitHub authentication sources (env-first, hosts-aware, optional keychain)

**Status:** Accepted  
**Date:** 2025-12-31

## Context
The project removed the `gh` CLI subprocess dependency to gain deterministic timeouts, retries, and avoid subprocess hangs.

## Decision
1) **Primary auth source:** environment variable `ISSUE_ORCH_GITHUB_TOKEN`
2) **Fallback:** `GITHUB_TOKEN`
3) **Supported convenience:** GitHub CLI `hosts.yml`
4) **Optional convenience:** `issue-orchestrator auth store` using OS keychain via `keyring`
5) **Not supported:** shelling out to `gh` for token discovery

## Consequences
- Simple onboarding
- Cross-platform
- Compatible with agent isolation
- Reuses a user's existing `gh auth login` session without subprocess dependence
