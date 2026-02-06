# Documentation

## By Audience

### For Users
Getting started with Issue Orchestrator:
- [Installation](user/installation.md)
- [Quickstart](user/quickstart.md)
- [Configuration (Quickstart)](user/configuration.md)
- [Configuration Reference](user/configuration_reference.md)
- [GitHub Token Setup](user/github-permissions.md)
- [Tutorial](user/tutorial.md)

### For Developers
Contributing to or extending Issue Orchestrator:
- [Testing Guide](development/TESTING.md)
- [Debugging Events](development/debugging.md)
- [Troubleshooting](development/TROUBLESHOOTING.md)
- [Review Workflow](development/REVIEW_WORKFLOW.md)
- [GitHub Token Setup](development/GITHUB_TOKEN_SETUP.md)

### For AI Agents
If you're an AI agent working on this codebase:
- Start with [`AI.md`](../AI.md) in the repo root
- Review [`AGENT_PROTOCOL.md`](../AGENT_PROTOCOL.md) for completion contracts
- Check CLAUDE.md files in `src/` and `tests/` for context-specific guidance

## Architecture

System design and decision records:
- [Architecture Overview](architecture/README.md) - Core architecture and boundaries
- [Architecture Decision Records](architecture/ADR/README.md) - Why decisions were made
- [Hooks & Guardrails](architecture/hooks.md) - Multi-layer safety enforcement
- [Validation System](architecture/validation.md) - Publish gate design
