"""One bounded artifact contract for custody publication and consumption."""

import json
import re
from hashlib import sha256

from .completion_intake import CompletionIntakeError, OwnedValidationResult

CUSTODY_ARTIFACT_LIMIT = 4 * 1024 * 1024
_ARTIFACT_NAMES = frozenset(
    {
        "raw.json",
        "completion.json",
        "validation.json",
        "stdout.log",
        "stderr.log",
    }
)


def require_custody_artifact(name: str, data: bytes) -> None:
    if (
        name not in _ARTIFACT_NAMES
        and re.fullmatch(r"(?:stdout|stderr)\.log\.part-[0-9]{8}", name) is None
    ):
        raise CompletionIntakeError("unknown custody artifact")
    require_custody_size(data)


def require_custody_size(data: bytes) -> None:
    if len(data) > CUSTODY_ARTIFACT_LIMIT:
        raise CompletionIntakeError("artifact exceeds custody limit")


def validation_output_exceeds_limit(stdout: bytes, stderr: bytes) -> bool:
    return max(len(stdout), len(stderr)) > CUSTODY_ARTIFACT_LIMIT


def validation_custody_blobs(result: OwnedValidationResult) -> dict[str, bytes]:
    """Keep complete oversized diagnostics in ordered, hashed, bounded parts.

    The ordinary log path becomes an explicit failure descriptor, never a
    truncated log. Each part is independently verified by the custody reader.
    """
    if result.passed and validation_output_exceeds_limit(
        result.stdout_bytes, result.stderr_bytes
    ):
        raise CompletionIntakeError("oversized validation output cannot pass")
    blobs = {"validation.json": result.result_bytes}
    for name, data in (
        ("stdout.log", result.stdout_bytes),
        ("stderr.log", result.stderr_bytes),
    ):
        if len(data) <= CUSTODY_ARTIFACT_LIMIT:
            blobs[name] = data
            continue
        parts = []
        for index, offset in enumerate(range(0, len(data), CUSTODY_ARTIFACT_LIMIT)):
            part_name = f"{name}.part-{index:08d}"
            chunk = data[offset : offset + CUSTODY_ARTIFACT_LIMIT]
            blobs[part_name] = chunk
            parts.append(
                {
                    "path": part_name,
                    "byte_size": len(chunk),
                    "sha256": sha256(chunk).hexdigest(),
                }
            )
        blobs[name] = json.dumps(
            {
                "format": "completion-validation-output-parts-v1",
                "failure": "validation output exceeds custody artifact limit",
                "byte_size": len(data),
                "sha256": sha256(data).hexdigest(),
                "artifact_limit": CUSTODY_ARTIFACT_LIMIT,
                "parts": parts,
            },
            sort_keys=True,
        ).encode()
    return blobs
