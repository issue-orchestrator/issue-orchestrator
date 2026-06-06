# Release Process

Use the release script from a clean checkout on the commit you intend to
release. The script prints its plan, asks you to type the exact tag, then runs
the workflow and fails fast on the first problem:

```bash
make release VERSION=v1.0.0
```

The full release flow:

1. Requires a clean git worktree.
2. Fetches `origin/main` and tags.
3. Requires the current branch to be `main`.
4. Requires local `HEAD` to exactly match fetched `origin/main`.
5. Verifies the local tag, remote tag, and GitHub release do not already exist.
6. Updates `pyproject.toml`, refreshes `uv.lock`, and syncs `.venv`.
7. Verifies installed package metadata so Control Center shows the release version.
8. Runs `make validate-pr`.
9. Fails if anything except `pyproject.toml` and `uv.lock` changed.
10. Commits `pyproject.toml` and `uv.lock` if release metadata changed.
11. Creates the annotated tag, pushes `HEAD:main --follow-tags`, and creates the GitHub release.

`VERSION` may be passed with or without the leading `v`; package metadata stores
the plain form (`1.0.0`) and release tags use the prefixed form (`v1.0.0`).

The Control Center footer renders `v{{ version }}` from
`importlib.metadata.version("issue-orchestrator")` via
`resolve_runtime_identity()`. The prep script refreshes `uv.lock`, runs
`uv sync --frozen --all-extras`, and verifies the local `.venv` metadata so a
restarted Control Center displays the same release version in the sidebar
footer.

To preview without changing files:

```bash
make release VERSION=v1.0.0 ARGS=--dry-run
```

Dry-run still runs the read-only release preflight checks, including the clean
worktree check, current branch check, local `HEAD` versus remote `origin/main`
comparison via `git ls-remote`, tag existence checks, and optional GitHub
release existence checks.

If you only want to update the files and skip the rest of the release workflow:

```bash
make prepare-release VERSION=v1.0.0
```

For automation, use `ARGS=--yes` to skip the exact-tag confirmation prompt.
