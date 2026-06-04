# Ports

**Purpose**: Abstract interfaces (protocols) that define boundaries between layers.

**Boundaries**:
- Pure interfaces: `Protocol` classes with no implementation
- Domain and control layers depend on ports, never on adapters
- Adapters implement these interfaces
- Enables swapping implementations (e.g., tmux ↔ custom terminal) without touching business logic

## Run-Asset Port Contracts

- Active session/completion/review ports should accept or return typed run-asset
  contracts, not optional paths or loose dictionaries.
- A port method that performs best-effort historical lookup must be named and
  documented as inspection-only. It must not be used by active control paths.
- Protocol signatures should make required data impossible to omit at compile
  time wherever Python typing allows it.
