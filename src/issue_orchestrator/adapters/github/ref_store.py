"""Reusable GitHub Git-ref compare-and-swap storage.

One logical record lives in one ref. The ref points at a commit whose TREE
carries the record as a blob, and whose parent is the previously observed
commit. GitHub's non-force fast-forward check is therefore the compare-and-swap
boundary, unchanged.

The record used to live in the commit MESSAGE, and that was a silent size trap
(#7272): GitHub's Git Data API truncates ``message`` at exactly 65 536
characters and appends an ellipsis. A record above that could still be WRITTEN
-- the create call accepts the whole thing -- and could never be read back, so
one repository's registry became unreadable while the write that broke it
reported success.

A blob has no such cap at these sizes, and this module still refuses a payload
larger than :data:`MAX_RECORD_BYTES` at WRITE time rather than discovering it on
the next read. Records written before this change are still read, from the
message, so nothing has to be migrated before it can be opened -- unless that
message is at the cap, in which case the read RAISES. Handing back GitHub's
truncated representation is what let a consumer treat a partial record as a
whole one, and refusing it is the only answer a storage layer can honestly give.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import GitHubHttpError

if TYPE_CHECKING:
    from .http_client import GitHubHttpClient


#: Where the record lives inside the commit's tree.
RECORD_PATH = "record.json"

#: Declares, in the commit this module writes, that the record is in the TREE.
#:
#: The presence of ``record.json`` cannot be the discriminator. A pre-#7272
#: commit reused the DEFAULT BRANCH's root tree, so a repository that happens to
#: keep a root ``record.json`` would have that unrelated file read back as its
#: registry -- valid-looking and wrong. The marker is written by this module, is
#: a fixed size, and therefore can never be the thing that truncates.
RECORD_FORMAT_MARKER = "io-record-format: tree-blob-v1"

#: Where GitHub's Git Data API stops returning a commit message, to the
#: character. A legacy record this long was cut off in transit, and no amount of
#: re-reading through the API will produce the rest of it.
API_MESSAGE_CAP = 65536

#: Blob mode for a regular file, as the Git Data tree API spells it.
_BLOB_MODE = "100644"

#: A sanity bound on one record: two orders of magnitude above the largest
#: registry observed (105 KB / 53 patterns) and far below the 100 MB the blob
#: API accepts. It stops a runaway writer, but it is NOT what makes this storage
#: safe -- safety comes from every read-path failure being loud. #7272 was not
#: caused by a missing size check; it was caused by a size overrun that looked
#: exactly like a valid record on the way back.
MAX_RECORD_BYTES = 8 * 1024 * 1024


class RecordTooLargeError(ValueError):
    """A record was larger than this storage will accept."""


class RecordTruncatedError(ValueError):
    """A legacy record was cut off by the API and cannot be read back.

    Raised rather than returned, because a truncated record is not a shorter
    record: a claim payload cut inside its newest block still parses, and the
    reader gets the PREVIOUS claim as though it were current. The intact object
    is still in git, so this is recoverable -- see
    ``scripts/republish_pattern_registry_record.py``.
    """


@dataclass(frozen=True)
class GitRefSnapshot:
    """One exact ref value and the record it carries."""

    ref: str
    commit_sha: str
    tree_sha: str
    record: str


class GitRefCasStore:
    """Opaque one-record-per-ref storage with atomic create and update."""

    def __init__(self, client: "GitHubHttpClient", *, ref_prefix: str) -> None:
        self._client = client
        self._ref_prefix = ref_prefix.rstrip("/")
        self._default_branch: str | None = None

    def ref_for(self, key: str) -> str:
        return f"{self._ref_prefix}/{key}"

    def read(self, key: str) -> GitRefSnapshot | None:
        ref = self.ref_for(key)
        ref_payload = self._client.get_git_ref(ref)
        if ref_payload is None:
            return None
        commit_sha = payload_commit_sha(ref_payload)
        commit_payload = self._client.get_git_commit(commit_sha)
        tree_sha = payload_tree_sha(commit_payload)
        message = str(commit_payload.get("message") or "")
        record = (
            self._record_from_tree(tree_sha)
            if RECORD_FORMAT_MARKER in message
            else _legacy_record(message, commit_sha=commit_sha)
        )
        return GitRefSnapshot(
            ref=ref,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            record=record,
        )

    def _record_from_tree(self, tree_sha: str) -> str:
        """The record blob's content. The commit said it is here, so it is."""
        tree = self._client.get_git_tree(tree_sha)
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise ValueError(f"GitHub tree payload has no entries: {tree}")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("path") != RECORD_PATH:
                continue
            blob_sha = entry.get("sha")
            if not isinstance(blob_sha, str) or not blob_sha:
                raise ValueError(f"{RECORD_PATH} tree entry has no sha: {entry}")
            return _decode_blob(self._client.get_git_blob(blob_sha))
        # Never a fall-back to the message: the commit declared the tree format,
        # so a missing blob is a broken record, not an old one. A truncated
        # listing lands here too, and cannot prove the blob is absent either.
        raise ValueError(
            f"tree {tree_sha} carries no {RECORD_PATH} although its commit "
            f"declares {RECORD_FORMAT_MARKER!r}"
            + (" (GitHub truncated the listing)" if tree.get("truncated") else "")
        )

    def _record_tree_sha(self, record: str) -> str:
        """Store the record as a blob and return the tree that carries it."""
        size = len(record.encode("utf-8"))
        if size > MAX_RECORD_BYTES:
            raise RecordTooLargeError(
                f"record is {size} bytes, above the {MAX_RECORD_BYTES}-byte "
                "limit this storage accepts; refusing to write a record that "
                "could not be read back"
            )
        blob = self._client.create_git_blob(content=record)
        tree = self._client.create_git_tree(
            tree=[
                {
                    "path": RECORD_PATH,
                    "mode": _BLOB_MODE,
                    "type": "blob",
                    "sha": payload_sha(blob),
                }
            ]
        )
        return payload_sha(tree)

    def create(self, key: str, record: str) -> bool:
        default_branch = self._default_branch_name()
        base_ref = self._client.get_git_ref(f"refs/heads/{default_branch}")
        if base_ref is None:
            raise ValueError(
                f"default branch ref refs/heads/{default_branch} was not found"
            )
        base_sha = payload_commit_sha(base_ref)
        ref = self.ref_for(key)
        commit = self._client.create_git_commit(
            message=commit_summary(ref),
            tree_sha=self._record_tree_sha(record),
            parents=[base_sha],
        )
        try:
            self._client.create_git_ref(ref=ref, sha=payload_sha(commit))
            return True
        except GitHubHttpError as exc:
            if is_ref_conflict(exc):
                return False
            raise

    def update(self, snapshot: GitRefSnapshot, record: str) -> bool:
        commit = self._client.create_git_commit(
            message=commit_summary(snapshot.ref),
            tree_sha=self._record_tree_sha(record),
            parents=[snapshot.commit_sha],
        )
        try:
            self._client.update_git_ref(
                ref=snapshot.ref, sha=payload_sha(commit), force=False
            )
            return True
        except GitHubHttpError as exc:
            if is_ref_conflict(exc):
                return False
            raise

    def delete(self, snapshot: GitRefSnapshot) -> bool:
        """Delete the ref, refusing when it no longer holds ``snapshot``.

        GitHub's Git Data API has no conditional delete, so this re-reads and
        compares rather than compare-and-swapping. That NARROWS the window in
        which a deleter holding a stale snapshot removes a successor's ref -- it
        does not close it -- but an unconditional delete had no window at all:
        it always removed whatever was there. Returns False when the ref has
        moved on, which every caller already treats as "not mine to release".
        """
        current = self._client.get_git_ref(snapshot.ref)
        if current is None:
            return True
        if payload_commit_sha(current) != snapshot.commit_sha:
            return False
        try:
            self._client.delete_git_ref(snapshot.ref)
            return True
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return True
            raise

    def _default_branch_name(self) -> str:
        if self._default_branch is None:
            self._default_branch = self._client.get_default_branch()
        return self._default_branch


def commit_summary(ref: str) -> str:
    """The commit message for a tree-backed record.

    Deliberately short and fixed-size: it carries a summary for a person and
    :data:`RECORD_FORMAT_MARKER` for a reader, and nothing whose length depends
    on the record -- so it can never become the place a record quietly outgrows
    again (#7272).
    """
    return f"Update {ref} ({RECORD_PATH})\n\n{RECORD_FORMAT_MARKER}"


def _legacy_record(message: str, *, commit_sha: str) -> str:
    """A record written before #7272, read from the message it lives in."""
    if len(message) >= API_MESSAGE_CAP:
        raise RecordTruncatedError(
            f"the record on commit {commit_sha} is {len(message)} characters, "
            f"at GitHub's {API_MESSAGE_CAP}-character commit-message cap, so "
            "what the API returned is a prefix of it; refusing to read a "
            "partial record as a whole one. Recover it with "
            "scripts/republish_pattern_registry_record.py"
        )
    return message


def _decode_blob(blob: dict) -> str:
    """The blob's text, however GitHub chose to encode it."""
    content = blob.get("content")
    if not isinstance(content, str):
        raise ValueError(f"GitHub blob payload has no content: {blob}")
    encoding = blob.get("encoding")
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8")
    if encoding in (None, "utf-8"):
        return content
    # Including the encoding GitHub uses when it declines to inline a blob
    # ("none"): a record we cannot decode is an error, never a partial answer.
    raise ValueError(f"unsupported GitHub blob encoding: {encoding!r}")


def payload_sha(payload: dict) -> str:
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"GitHub payload missing sha: {payload}")
    return sha


def payload_commit_sha(payload: dict) -> str:
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise ValueError(f"GitHub ref payload missing object: {payload}")
    sha = obj.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"GitHub ref payload missing object.sha: {payload}")
    return sha


def payload_tree_sha(payload: dict) -> str:
    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise ValueError(f"GitHub commit payload missing tree: {payload}")
    sha = tree.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"GitHub commit payload missing tree.sha: {payload}")
    return sha


def is_ref_conflict(exc: GitHubHttpError) -> bool:
    return exc.status_code in {409, 422}
