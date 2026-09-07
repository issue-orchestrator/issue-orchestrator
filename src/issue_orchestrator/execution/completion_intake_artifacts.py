"""Atomic self-describing custody envelopes, exclusively in owner state storage."""

import json
import os
import stat
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..domain.completion_intake import CompletionIntakeError


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(path: Path, *, limit: int = 4 * 1024 * 1024) -> bytes:
    """Open each component without following symlinks, then bound the read."""
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise CompletionIntakeError("artifact path must be absolute and normalized")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        file_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise CompletionIntakeError("artifact is not a bounded regular file")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise CompletionIntakeError("artifact exceeds custody limit")
            return data
    finally:
        os.close(fd)


class CompletionIntakeArtifacts:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.resolve() != root or root.is_symlink():
            raise CompletionIntakeError(
                "custody root must be canonical and owner controlled"
            )
        sync_directory(root.parent)

    def write(
        self, key: str, metadata: dict[str, object], blobs: dict[str, bytes]
    ) -> None:
        final = self.root / key
        envelope = dict(metadata)
        envelope["blobs"] = {
            name: sha256(data).hexdigest() for name, data in blobs.items()
        }
        if final.exists():
            if self.read(key) != envelope:
                raise CompletionIntakeError("immutable custody envelope conflict")
            return
        stage = self.root / (".staging-" + uuid4().hex)
        stage.mkdir(mode=0o700)
        for name, data in {**blobs, "envelope.json": canonical_bytes(envelope)}.items():
            with (stage / name).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        sync_directory(stage)
        os.rename(stage, final)
        sync_directory(self.root)

    def read(self, key: str) -> dict[str, object]:
        if len(key) not in (64, 75) or "/" in key or ".." in key:
            raise CompletionIntakeError("invalid custody key")
        envelope = json.loads(read_regular(self.root / key / "envelope.json"))
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("blobs"), dict
        ):
            raise CompletionIntakeError("invalid custody envelope")
        for name, digest in envelope["blobs"].items():
            if name not in {
                "raw.json",
                "completion.json",
                "validation.json",
                "stdout.log",
                "stderr.log",
            }:
                raise CompletionIntakeError("unknown custody artifact")
            if sha256(read_regular(self.root / key / name)).hexdigest() != digest:
                raise CompletionIntakeError("custody artifact hash mismatch")
        return envelope

    def envelopes(self) -> tuple[str, ...]:
        # Only this owner's envelopes: never an agent run directory or manifest.
        return tuple(
            sorted(
                path.name
                for path in self.root.iterdir()
                if not path.name.startswith((".staging-", ".validation-"))
            )
        )
