"""Git observations and owned checkout allocation for budgeted suites."""

from pathlib import Path
import hashlib
import re

from ..ports.command_runner import CommandRunner


class BudgetedValidationGit:
    def __init__(self, root: Path, runner: CommandRunner) -> None:
        self._root = root
        self._runner = runner

    def git(self, *arguments: str) -> str:
        result = self._runner.run(["git", *arguments], cwd=self._root, timeout_seconds=120)
        if result.returncode:
            raise RuntimeError(f"Budgeted validation Git operation failed: {result.stderr}")
        return result.stdout.strip()

    def storage_directory(self) -> Path:
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "io-budgeted-validation"

    def head(self, branch: str) -> str:
        # The cycle lease owns this private ref. Unrelated fetches may rewrite
        # FETCH_HEAD, so it cannot identify the snapshot being validated.
        ref = f"refs/io-budgeted-validation/{hashlib.sha256(branch.encode()).hexdigest()}"
        self.git("fetch", "--no-tags", "--no-write-fetch-head", "origin", f"+refs/heads/{branch}:{ref}")
        return self.git("rev-parse", f"{ref}^{{commit}}")

    def changes(self, good: str, bad: str) -> tuple[str, ...]:
        if good == bad:
            return ()
        first_parent = tuple(self.git("rev-list", "--first-parent", bad).splitlines())
        if good not in first_parent:
            raise ValueError("Successful validation commit is no longer on this branch's first-parent history")
        return tuple(reversed(first_parent[:first_parent.index(good)]))

    def merged_count(self, commits: tuple[str, ...]) -> int:
        if not commits:
            return 0
        subjects = self.git("show", "-s", "--format=%s", *commits).splitlines()
        prs: set[str] = set()
        untagged = 0
        for subject in subjects:
            match = re.search(r"\(#(\d+)\)$|^Merge pull request #(\d+)\b", subject)
            if match:
                prs.add(next(value for value in match.groups() if value is not None))
            else:
                untagged += 1
        return len(prs) + untagged

    def create_checkout(self, commit: str, path: Path) -> None:
        if path.exists():
            raise ValueError("Budgeted validation requires a fresh owned checkout")
        self.git("worktree", "add", "--detach", str(path), commit)

    def remove_checkout(self, path: Path) -> None:
        self.git("worktree", "remove", "--force", str(path))
