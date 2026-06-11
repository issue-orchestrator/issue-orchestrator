# Release Process

Use the release scripts in two operator steps so the version bump goes through
normal branch protection before the tag is published.

First create the release metadata PR:

```bash
make release-pr VERSION=v1.0.0
```

The script fetches `origin/main`, creates a `release-v1.0.0` branch from it,
updates `pyproject.toml` and `uv.lock`, syncs `.venv`, commits the release
metadata with signoff, runs `make validate-pr`, pushes the branch, and opens the
pull request. On success, your checkout remains on the release PR branch. If a
step fails after that branch is created, the script prints local and remote
branch cleanup commands before retrying.

Review and merge that PR to `main` through the normal gate.

`make release-pr` leaves your checkout on the release PR branch, and the final
release requires local `main` to match the merged `origin/main` exactly. So
after the PR merges, switch back and fast-forward to the merge commit:

```bash
git switch main && git pull --ff-only origin main
```

Then run the final release from that clean, up-to-date checkout:

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
`resolve_runtime_identity()`. `make release-pr` refreshes `uv.lock`, runs
`uv sync --frozen --all-extras`, and verifies the local `.venv` metadata. The
final `make release` command repeats the environment sync and metadata
verification from the merged `main` commit so a restarted Control Center
displays the same release version in the sidebar footer.

To preview release PR creation without changing files:

```bash
make release-pr VERSION=v1.0.0 ARGS=--dry-run
```

To preview final release publishing without changing files:

```bash
make release VERSION=v1.0.0 ARGS=--dry-run
```

Dry-run still runs read-only preflight checks. For `release-pr`, that includes
the clean worktree check, `origin/main` lookup, branch and tag collision checks,
and optional GitHub release checks. For final release publishing, it also checks
the current branch, local `HEAD` versus remote `origin/main`, and merged release
metadata versions.

`make prepare-release VERSION=v1.0.0` remains available as a lower-level file
prep command when you intentionally want to edit, commit, push, and open the PR
yourself.

If `gh release create` fails after the tag push, do not rerun the full release
command unchanged because the remote tag now exists. Create the GitHub release
from the pushed tag:

```bash
gh release create v1.0.0 --generate-notes
```

For automation, use `ARGS=--yes` to skip the exact-tag confirmation prompt.
