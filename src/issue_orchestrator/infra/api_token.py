"""Bearer-token resolution for the Control API.

The Control API binds to 127.0.0.1 but is otherwise reachable from any
process running as the same user. A shared-secret token gates mutating
routes so that a stray local process (or a misconfigured non-loopback
bind) cannot issue ``/control/orchestrator/start``, request shutdowns,
subscribe to the SSE stream, etc.

Resolution order:

1. ``ISSUE_ORCHESTRATOR_API_TOKEN`` env var (used by agent subprocesses,
   tests, and operators that want to inject their own value).
2. ``~/.issue-orchestrator/api-token`` file — auto-generated on first
   startup with mode ``0600``. Reused across restarts so clients do not
   need to rediscover the token each boot.

The token is a 32-byte hex string (256 bits of entropy). Comparisons use
``hmac.compare_digest`` to avoid timing side channels.

See security issue #5987 (F3).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "ISSUE_ORCHESTRATOR_API_TOKEN"
_DEFAULT_TOKEN_RELATIVE = Path(".issue-orchestrator") / "api-token"


def default_token_path() -> Path:
    """Return the default on-disk location for the Control API token."""
    return Path.home() / _DEFAULT_TOKEN_RELATIVE


def generate_token() -> str:
    """Generate a fresh 32-byte hex token (256 bits)."""
    return secrets.token_hex(32)


def load_or_create_token(path: Path | None = None) -> str:
    """Load the token from ``path``, generating it if missing.

    On creation the file is written atomically with mode ``0600``. On
    read, non-0600 permissions are logged as a warning but still honored
    to avoid locking operators out of a running orchestrator because of
    an incidental chmod.
    """
    resolved = path or default_token_path()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if resolved.exists():
        token = resolved.read_text().strip()
        if token:
            mode = resolved.stat().st_mode & 0o777
            if mode != 0o600:
                logger.warning(
                    "Control API token file %s has permissions %o "
                    "(expected 0600). Tightening.",
                    resolved,
                    mode,
                )
                try:
                    resolved.chmod(0o600)
                except OSError as exc:
                    logger.warning(
                        "Could not tighten permissions on %s: %s",
                        resolved,
                        exc,
                    )
            return token
        # Empty file — fall through and regenerate.
        logger.warning(
            "Control API token file %s was empty; regenerating.", resolved
        )

    token = generate_token()
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    tmp.write_text(token)
    tmp.chmod(0o600)
    tmp.replace(resolved)
    logger.info("Generated Control API token at %s", resolved)
    return token


def resolve_api_token(path: Path | None = None) -> str:
    """Return the active Control API token.

    The env var wins when set (tests, operator overrides). Otherwise the
    on-disk file is loaded (or created).
    """
    from_env = os.environ.get(TOKEN_ENV_VAR)
    if from_env:
        return from_env
    return load_or_create_token(path)


def verify_token(expected: str, provided: str | None) -> bool:
    """Constant-time comparison of bearer token values."""
    if not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
