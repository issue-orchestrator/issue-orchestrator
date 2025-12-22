**Audience:** Design document (public). Not a usage guide.

# Validation Model

Validation is a **publish gate**, not a CI system.

- Run one user-defined local command per suite
- Cache results by worktree + commit SHA
- Reuse across feedback/publish/hooks
- Observe GitHub CI rather than reproducing it locally
