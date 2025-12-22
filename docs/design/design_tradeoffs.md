**Audience:** Design document (public). Not a usage guide.

# Design Tradeoffs

- We rely on **affirmative sandbox verification** rather than tool-specific hooks to scale across AI systems.
- Hardened mode is opt-in because OS-level isolation requires privileged setup.
- Validation is a single user-defined command to avoid re-implementing task runners.
- We do not run GitHub CI locally; CI is authoritative server-side.
