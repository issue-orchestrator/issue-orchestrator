#!/usr/bin/env python3
"""Move a shared pattern-registry record out of its commit message into a blob.

#7272: the record used to live in the commit MESSAGE, and GitHub's Git Data API
truncates that field at 65 536 characters. `porchpin/porchpin`'s registry passed
that size (104 787 B / 53 entries), so every client reads back invalid JSON and
fails closed. The object itself is intact -- only the API read is lossy -- so
the record can be recovered from any clone that can fetch the ref.

This does the recovery with git plumbing rather than the API: it reads the exact
commit message from the local object, writes it as the `record.json` blob the
store now reads, and pushes a child commit onto the same ref. The push is an
ordinary fast-forward, so it takes part in the same compare-and-swap every other
writer uses -- it cannot clobber a concurrent write.

    # inspect only; nothing is created or pushed
    python scripts/republish_pattern_registry_record.py --repo-root ~/dev/porchpin

    # do it
    python scripts/republish_pattern_registry_record.py \
        --repo-root ~/dev/porchpin --apply

Exit status is 0 when the ref already carries a blob-backed record, when a dry
run finds a recoverable one, or when an --apply republished it with every entry
intact. Anything else is a non-zero failure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from issue_orchestrator.adapters.github.pattern_registry import (  # noqa: E402
    PATTERN_REGISTRY_REF_KEY,
    PATTERN_REGISTRY_REF_PREFIX,
    parse_entries,
)
from issue_orchestrator.adapters.github.ref_store import (  # noqa: E402
    API_MESSAGE_CAP,
    MAX_RECORD_BYTES,
    RECORD_FORMAT_MARKER,
    RECORD_PATH,
    commit_summary,
)

DEFAULT_REF = f"{PATTERN_REGISTRY_REF_PREFIX}/{PATTERN_REGISTRY_REF_KEY}"


class RepublishError(RuntimeError):
    """The record could not be recovered, and nothing was pushed."""


def read_entries(record: str, *, source: str) -> dict[str, object]:
    """The record's entries, or a refusal naming where the bad record came from.

    Every way a record can be unreadable arrives here, so the script never exits
    on a traceback the operator has to interpret.
    """
    try:
        return dict(parse_entries(record))
    except Exception as exc:
        raise RepublishError(f"{source} could not be read: {exc}") from exc


def git(repo_root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=stdin,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RepublishError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def commit_message(repo_root: Path, commit_sha: str) -> str:
    """The commit's message, byte-exact.

    Read from the raw object rather than a `--format` placeholder so nothing
    reformats the payload on its way out; this text has to hash to the same
    record the registry wrote.
    """
    raw = git(repo_root, "cat-file", "commit", commit_sha)
    header, _, message = raw.partition(b"\n\n")
    if not header or not message:
        raise RepublishError(f"commit {commit_sha} has no message")
    return message.decode("utf-8")


def _text(output: bytes) -> str:
    return output.decode().strip()


def is_tree_backed(repo_root: Path, commit_sha: str) -> bool:
    """Whether the commit declares that its record lives in its tree.

    The same discriminator the store reads, imported rather than re-derived: a
    pre-#7272 commit reused the default branch's ROOT tree, so the presence of
    a ``record.json`` path proves nothing about which format a commit is in.
    """
    return RECORD_FORMAT_MARKER in commit_message(repo_root, commit_sha)


def record_blob_sha(repo_root: Path, commit_sha: str) -> str:
    """The sha of a tree-backed commit's ``record.json``."""
    listing = git(repo_root, "ls-tree", f"{commit_sha}^{{tree}}").decode()
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        if path == RECORD_PATH:
            return meta.split()[2]
    raise RepublishError(
        f"commit {commit_sha} declares the tree format but carries no "
        f"{RECORD_PATH}"
    )


def republish(
    repo_root: Path, *, remote: str, ref: str, apply: bool
) -> tuple[str, int]:
    """Recover ``ref``'s record onto a blob. Returns (outcome, entry count)."""
    git(repo_root, "fetch", "--no-tags", remote, ref)
    commit_sha = _text(git(repo_root, "rev-parse", "FETCH_HEAD"))

    if is_tree_backed(repo_root, commit_sha):
        blob_sha = record_blob_sha(repo_root, commit_sha)
        record = git(repo_root, "cat-file", "blob", blob_sha).decode("utf-8")
        entries = read_entries(record, source=f"the {RECORD_PATH} blob on {ref}")
        return "already blob-backed", len(entries)

    record = commit_message(repo_root, commit_sha)
    size = len(record.encode("utf-8"))
    if size > MAX_RECORD_BYTES:
        raise RepublishError(
            f"record is {size} bytes, above the {MAX_RECORD_BYTES}-byte limit "
            "the store accepts"
        )
    if len(record) == API_MESSAGE_CAP:
        # EXACTLY the cap, not merely past it: a longer message is the intact
        # original, which is the whole reason this script reads local objects.
        # A message of exactly this length is indistinguishable from a copy the
        # API already cut off, so there is nothing here to recover from.
        raise RepublishError(
            f"the commit message on {commit_sha[:12]} is {len(record)} "
            f"characters, exactly GitHub's {API_MESSAGE_CAP}-character cap; "
            "this clone holds a truncated copy, not the original"
        )
    # Fails loudly on a record this clone cannot read either -- which would mean
    # the local object is ALSO short, and there is nothing here to recover.
    entries = read_entries(
        record, source=f"the commit message on {commit_sha[:12]}"
    )

    if not apply:
        return "recoverable", len(entries)

    new_commit = blob_backed_commit(
        repo_root, record=record, parent=commit_sha, ref=ref
    )

    # Not forced, and that is the whole safety argument: the new commit's only
    # parent is the one this recovery read, so git accepts the push exactly
    # while the ref still points there. A writer that moved it first wins and
    # this run fails, rather than discarding their record.
    git(repo_root, "push", remote, f"{new_commit}:{ref}")

    # Read the ref back from the remote rather than trusting the push. The
    # count this reports is therefore the remote's, which is the only one an
    # operator closing #7272 cares about.
    published = published_record(repo_root, remote=remote, ref=ref)
    if published is None:
        raise RepublishError(
            f"{ref} is not tree-backed after the push; it was rewritten"
        )
    republished = read_entries(published, source=f"{ref} after the push")
    if republished != entries:
        raise RepublishError(
            f"{ref} carries a different record after the push "
            f"({len(republished)} entries, not {len(entries)})"
        )
    return "republished", len(republished)


def blob_backed_commit(
    repo_root: Path, *, record: str, parent: str, ref: str
) -> str:
    """A child of ``parent`` carrying ``record`` exactly where the store reads it."""
    blob_sha = _text(
        git(repo_root, "hash-object", "-w", "--stdin", stdin=record.encode("utf-8"))
    )
    tree_sha = _text(
        git(
            repo_root,
            "mktree",
            stdin=f"100644 blob {blob_sha}\t{RECORD_PATH}\n".encode(),
        )
    )
    # The summary comes from the store, so a recovered commit is indistinguishable
    # from one the engine wrote.
    return _text(
        git(
            repo_root,
            "commit-tree",
            tree_sha,
            "-p",
            parent,
            "-m",
            commit_summary(ref),
        )
    )


def published_record(repo_root: Path, *, remote: str, ref: str) -> str | None:
    """``ref``'s record as the remote now holds it, or None if not tree-backed."""
    git(repo_root, "fetch", "--no-tags", remote, ref)
    commit_sha = _text(git(repo_root, "rev-parse", "FETCH_HEAD"))
    if not is_tree_backed(repo_root, commit_sha):
        return None
    blob_sha = record_blob_sha(repo_root, commit_sha)
    return git(repo_root, "cat-file", "blob", blob_sha).decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", required=True, type=Path, help="a clone that can fetch the ref"
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create and push the blob-backed commit (default: inspect only)",
    )
    args = parser.parse_args(argv)

    try:
        outcome, entry_count = republish(
            args.repo_root.expanduser(),
            remote=args.remote,
            ref=args.ref,
            apply=args.apply,
        )
    except RepublishError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"{args.ref}: {outcome} ({entry_count} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
