"""Identity of the configured validation that authorizes a retained completion."""

import hashlib
import json

from .completion_intake import CompletionIntakeError


def completion_validator_digest(command: str, timeout_seconds: int) -> str:
    payload = json.dumps({
        "suite": "completion_intake", "command": command,
        "timeout_seconds": timeout_seconds, "version": 1,
    }, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def require_current_completion_validator(attested_digest: str, command: str | None,
                                         timeout_seconds: int) -> None:
    """An old attestation cannot authorize a different or absent validator."""
    if not command:
        raise CompletionIntakeError("retained publication requires a configured validator")
    if attested_digest != completion_validator_digest(command, timeout_seconds):
        raise CompletionIntakeError("retained validation belongs to another configured validator")
