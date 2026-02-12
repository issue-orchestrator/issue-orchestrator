# Contributing

For dev setup, conventions, and how to make changes, see the [Developing journey](docs/journeys/developing.md).

## Running Tests from Forks

Some integration tests require a GitHub token and are skipped on fork PRs (GitHub blocks secrets from forks for security).

To run the full test suite in your fork:

1. Create a Personal Access Token at https://github.com/settings/tokens (minimal scopes needed)
2. In your fork: **Settings → Secrets and variables → Actions**
3. Add a secret named `GITHUB_TOKEN` with your PAT

Tests will then run when you push to your fork. Note: when you open a PR against the main repo, these tests will still skip on the PR checks, but will run after merge.
