"""An in-memory stand-in for the GitHub Git Database endpoints this repo uses.

Shared by every test of :mod:`ref_store` and the adapters built on it, so there
is one description of how GitHub behaves rather than one per test module.

It models two things faithfully on purpose:

* **Fast-forward ref updates.** ``update_git_ref`` refuses a non-force update
  whose commit does not descend from the current ref, which is the entire
  compare-and-swap boundary the store relies on.
* **The commit-message cap.** ``get_git_commit`` truncates ``message`` at
  :data:`FakeGitHubRefClient.MESSAGE_CAP` characters, exactly as the real API
  does. A fake without this let a record be written and never read back, which
  is how #7272 reached production unnoticed.
"""

from __future__ import annotations

import base64

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.ref_store import RECORD_PATH

BASE_COMMIT_SHA = "base"
BASE_TREE_SHA = "tree-base"


class FakeGitHubRefClient:
    """In-memory GitHub Git Database subset with fast-forward ref updates."""

    #: Where the real API stops returning a commit message, to the character.
    MESSAGE_CAP = 65536

    def __init__(self) -> None:
        self.refs: dict[str, str] = {"refs/heads/main": BASE_COMMIT_SHA}
        self.commits: dict[str, dict] = {
            BASE_COMMIT_SHA: {
                "sha": BASE_COMMIT_SHA,
                "message": "base commit",
                "tree": {"sha": BASE_TREE_SHA},
                "parents": [],
            }
        }
        self.trees: dict[str, dict] = {
            BASE_TREE_SHA: {"sha": BASE_TREE_SHA, "tree": []}
        }
        self.blobs: dict[str, str] = {}
        self.created_refs: list[tuple[str, str]] = []
        self.updated_refs: list[tuple[str, str, bool]] = []
        self.deleted_refs: list[str] = []
        self.conflict_updates_remaining = 0
        self.default_branch_reads = 0
        self._next_object = 1

    # -- refs ---------------------------------------------------------------

    def get_default_branch(self) -> str:
        self.default_branch_reads += 1
        return "main"

    def get_git_ref(self, ref: str) -> dict | None:
        sha = self.refs.get(ref)
        if sha is None:
            return None
        return {"ref": ref, "object": {"type": "commit", "sha": sha}}

    def create_git_ref(self, *, ref: str, sha: str) -> dict:
        if ref in self.refs:
            raise GitHubHttpError("ref exists", status_code=422)
        self.refs[ref] = sha
        self.created_refs.append((ref, sha))
        return {"ref": ref, "object": {"type": "commit", "sha": sha}}

    def update_git_ref(self, *, ref: str, sha: str, force: bool = False) -> dict:
        if self.conflict_updates_remaining:
            self.conflict_updates_remaining -= 1
            raise GitHubHttpError("conflict", status_code=409)
        current_sha = self.refs[ref]
        parents = self.commits[sha]["parents"]
        parent_sha = parents[0]["sha"] if parents else None
        if not force and parent_sha != current_sha:
            raise GitHubHttpError("not fast-forward", status_code=409)
        self.refs[ref] = sha
        self.updated_refs.append((ref, sha, force))
        return {"ref": ref, "object": {"type": "commit", "sha": sha}}

    def delete_git_ref(self, ref: str) -> None:
        if ref not in self.refs:
            raise GitHubHttpError("ref not found", status_code=404)
        del self.refs[ref]
        self.deleted_refs.append(ref)

    # -- commits ------------------------------------------------------------

    def get_git_commit(self, sha: str) -> dict:
        commit = dict(self.commits[sha])
        if len(commit["message"]) > self.MESSAGE_CAP:
            # What GitHub actually hands back: the head of the message and an
            # ellipsis, with no indication in the payload that it is partial.
            commit["message"] = commit["message"][: self.MESSAGE_CAP - 1] + "…"
        return commit

    def create_git_commit(
        self,
        *,
        message: str,
        tree_sha: str,
        parents: list[str],
    ) -> dict:
        sha = self._mint("commit")
        self.commits[sha] = {
            "sha": sha,
            "message": message,
            "tree": {"sha": tree_sha},
            "parents": [{"sha": parent} for parent in parents],
        }
        return self.commits[sha]

    # -- blobs and trees ----------------------------------------------------

    def create_git_blob(self, *, content: str) -> dict:
        sha = self._mint("blob")
        self.blobs[sha] = content
        return {"sha": sha}

    def get_git_blob(self, sha: str) -> dict:
        return {
            "sha": sha,
            "content": base64.b64encode(self.blobs[sha].encode()).decode(),
            "encoding": "base64",
        }

    def create_git_tree(self, *, tree: list[dict]) -> dict:
        sha = self._mint("tree")
        self.trees[sha] = {"sha": sha, "tree": list(tree)}
        return {"sha": sha}

    def get_git_tree(self, sha: str) -> dict:
        return self.trees[sha]

    # -- seeding ------------------------------------------------------------

    def seed_record(self, ref: str, record: str) -> str:
        """Point ``ref`` at a commit carrying ``record`` the way writes do now."""
        blob = self.create_git_blob(content=record)
        tree = self.create_git_tree(
            tree=[
                {
                    "path": RECORD_PATH,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            ]
        )
        return self._point(ref, message=f"Update {ref}", tree_sha=tree["sha"])

    def record_at(self, ref: str) -> str:
        """The record ``ref`` carries, read the way the store reads it."""
        commit = self.commits[self.refs[ref]]
        for entry in self.trees[commit["tree"]["sha"]]["tree"]:
            if entry["path"] == RECORD_PATH:
                return self.blobs[entry["sha"]]
        raise KeyError(f"{ref} carries no {RECORD_PATH}")

    def seed_legacy_message_record(self, ref: str, record: str) -> str:
        """Point ``ref`` at a commit carrying ``record`` in its MESSAGE.

        How every record was written before #7272, and what the read path still
        has to open without a migration step first.
        """
        return self._point(ref, message=record, tree_sha=BASE_TREE_SHA)

    # -- internals ----------------------------------------------------------

    def _point(self, ref: str, *, message: str, tree_sha: str) -> str:
        parent = self.refs.get(ref, BASE_COMMIT_SHA)
        commit = self.create_git_commit(
            message=message, tree_sha=tree_sha, parents=[parent]
        )
        if ref in self.refs:
            self.update_git_ref(ref=ref, sha=commit["sha"])
        else:
            self.create_git_ref(ref=ref, sha=commit["sha"])
        return commit["sha"]

    def _mint(self, kind: str) -> str:
        sha = f"{kind}-{self._next_object}"
        self._next_object += 1
        return sha
