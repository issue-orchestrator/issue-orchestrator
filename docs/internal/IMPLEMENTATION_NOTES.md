# Implementation Notes (internal)

**Audience:** AI agents and contributors implementing changes.

Start with:
- `planning/IMPLEMENTATION_TASK_LIST.md`
- `planning/TEST_CHECKLIST.md`

Guiding invariants:
- Agents must not gain publish authority.
- Orchestrator performs pushing/PR creation; humans merge.
- Sandbox verification is the system-agnostic backstop.
- Validation is a one-command publish gate with suite+SHA caching.
