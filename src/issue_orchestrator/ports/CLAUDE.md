# Ports

**Purpose**: Abstract interfaces (protocols) that define boundaries between layers.

**Boundaries**:
- Pure interfaces: `Protocol` classes with no implementation
- Domain and control layers depend on ports, never on adapters
- Adapters implement these interfaces
- Enables swapping implementations (tmux ↔ iTerm2) without touching business logic
