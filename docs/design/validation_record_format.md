**Audience:** Design document (public). Not a usage guide.

# Validation Record Format

Location:
`.issue-orchestrator/validation/<suite>/<HEAD_SHA>.json`

Record should include:
- schema_version
- suite
- head_sha
- passed + exit_code
- command
- started_at / ended_at
- stdout/stderr paths (optional but recommended)
