**Audience:** Design document (public). Not a usage guide.

# verify-agent-sandbox spec

Checks (minimum):
- forbidden env vars absent
- `gh auth status` fails
- `git push --dry-run` fails fast (no prompt)
- mode-specific: isolated HOME (standard) or `whoami` (hardened)

Bootstrap failure behavior:
- refuse to start agent session
- emit trace event `sandbox_verification_failed`
