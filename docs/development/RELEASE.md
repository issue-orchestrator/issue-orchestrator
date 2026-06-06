# Release Process

Use the release scripts in two steps so the version bump goes through normal
branch protection before the tag is published.

First prepare the version files, commit them, open a PR, and merge that PR to
`main`:

```bash
make prepare-release VERSION=v1.0.0
```

Then update local `main` to the merge commit and run the final release from a
clean checkout:

```bash
make release VERSION=v1.0.0
```

The full release flow:

1. Requires a clean git worktree.
2. Fetches `origin/main` and tags.
3. Requires the current branch to be `main`.
4. Requires local `HEAD` to exactly match fetched `origin/main`.
5. Verifies the local tag, remote tag, and GitHub release do not already exist.
6. Verifies `pyproject.toml` and `uv.lock` already contain the target version.
7. Syncs `.venv` and verifies installed package metadata so Control Center shows
   the release version.
8. Runs `make validate-pr`.
9. Re-checks that the worktree is clean.
10. Creates the annotated tag, pushes only that tag, and creates the GitHub release.

`VERSION` may be passed with or without the leading `v`; package metadata stores
the plain form (`1.0.0`) and release tags use the prefixed form (`v1.0.0`).

The Control Center footer renders `v{{ version }}` from
`importlib.metadata.version("issue-orchestrator")` via
`resolve_runtime_identity()`. `make prepare-release` refreshes `uv.lock`, runs
`uv sync --frozen --all-extras`, and verifies the local `.venv` metadata. The
final `make release` command repeats the environment sync and metadata
verification from the merged `main` commit so a restarted Control Center
displays the same release version in the sidebar footer.

To preview without changing files:

```bash
make release VERSION=v1.0.0 ARGS=--dry-run
```

Dry-run still runs the read-only release preflight checks, including the clean
worktree check, current branch check, local `HEAD` versus remote `origin/main`
comparison via `git ls-remote`, tag existence checks, release metadata version
checks, and optional GitHub release existence checks.

If `gh release create` fails after the tag push, do not rerun the full release
command unchanged because the remote tag now exists. Create the GitHub release
from the pushed tag:

```bash
gh release create v1.0.0 --generate-notes
```

For automation, use `ARGS=--yes` to skip the exact-tag confirmation prompt.
