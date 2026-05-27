# Contributing

For dev setup, conventions, and how to make changes, see the [Developing journey](docs/journeys/developing.md).

## License and Contribution Terms

Issue-Orchestrator is licensed under the Apache License, Version 2.0.
Unless you explicitly mark a contribution as "Not a Contribution", anything
you intentionally submit to this project is submitted under Apache-2.0.

This project does not require a Contributor License Agreement (CLA). If
commercial relicensing becomes a concrete plan in the future, contributor
agreements can be reconsidered then; they are not part of today's contribution
process.

There is no proprietary split in this repository. Contributions go to the
Apache-2.0 project unless a future governance change says otherwise.

The Issue-Orchestrator name, logos, and project marks are retained by Bruce
Gordon. The Apache-2.0 license grants rights to the code, not trademark or
brand rights except for reasonable and customary use in describing the origin
of the software.

## Developer Certificate of Origin

All commits must be signed off under the Developer Certificate of Origin 1.1:
https://developercertificate.org/

Add a sign-off by committing with `-s`:

```bash
git commit -s -m "Describe the change"
```

That appends a line like this to the commit message:

```text
Signed-off-by: Your Name <you@example.com>
```

The sign-off certifies that you wrote the contribution or otherwise have the
right to submit it under the project license. Use the same name and email as
the commit author whenever possible.

If you already made an unsigned commit, fix it before opening or updating the
pull request. Use `git commit --amend -s` when only the latest commit needs a
sign-off. Use `git rebase --signoff origin/main` when every commit in a
multi-commit branch needs a sign-off; it rewrites those branch commits and
changes their SHAs.

```bash
git commit --amend -s
git rebase --signoff origin/main
```

Pull requests must pass the repository DCO check before merge. Maintainers
should install the DCO2 GitHub App for this repository, keep `.github/dco.yml`
on the default branch, and require the emitted DCO status check in branch
protection. Also enable GitHub's repository setting to require sign-off for
web-based commits.

## Running Tests from Forks

Some integration tests require a GitHub token and are skipped on fork PRs (GitHub blocks secrets from forks for security).

To run the full test suite in your fork:

1. Create a Personal Access Token at https://github.com/settings/tokens (minimal scopes needed)
2. In your fork: **Settings → Secrets and variables → Actions**
3. Add a secret named `GITHUB_TOKEN` with your PAT

Tests will then run when you push to your fork. Note: when you open a PR against the main repo, these tests will still skip on the PR checks, but will run after merge.
