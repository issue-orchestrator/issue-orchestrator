# Entrypoints

**Purpose**: User-facing interfaces - CLI commands, web server, agent tools.

**Boundaries**:
- Thin layer: parse input, call into control/domain, format output
- No business logic - delegate to control layer
- `cli_tools/` contains tools agents use (e.g., `agent-done`)
- Web endpoints serve the dashboard and API
